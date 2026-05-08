"""Backtest harness (F1).

Replays the deterministic rules-based signal engine across historical OHLCV
bars and reports win-rate / expectancy / drawdown so we can quantify how the
strategy holds up before tweaking it again.

LLM providers are intentionally NOT exercised here:
  - they are non-deterministic (so backtest results would not be reproducible),
  - they cost money / API quota,
  - the rules-based engine is the floor of strategy behaviour anyway and is
    always run as the final guardrail in production via `_validate_signal`.

Usage (CLI)::

    python -m app.backtest --coin BTC --timeframe 1h --max-hold 48

The harness is also importable for tests::

    from app.backtest import simulate_trade, run_backtest
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .data import fetch_ohlcv
from .indicators import (
    IndicatorBundle,
    cluster_levels,
    compute_all,
    find_pivots,
)
from .llm import _rules_based_fallback
from .schemas import Signal

log = logging.getLogger(__name__)

# Minimum number of warmup bars before we can trust indicators (slowest EMA is
# 200; we need a few extra bars on top to avoid the noisy ramp-up).
WARMUP_BARS = 220

# Default cap on how long we'll wait for a limit/stop order to be triggered
# after the signal bar. Mirrors the practical "if it didn't fill in N bars,
# the setup is stale" heuristic.
DEFAULT_MAX_WAIT = 5

# Default cap on how many bars we'll hold a triggered position before we
# bail at market. Prevents 1h strategies from accidentally turning into
# month-long swing trades when neither SL nor TP gets hit.
DEFAULT_MAX_HOLD = 48


@dataclass
class Trade:
    """One backtested trade — either triggered or filtered out."""

    direction: str  # "long" | "short"
    entry_type: str  # "market" | "limit" | "stop"
    signal_idx: int
    signal_ts: pd.Timestamp
    planned_entry: float
    stop_loss: float
    take_profit: float
    confidence: int
    rr_planned: Optional[float]

    triggered: bool
    entry_idx: Optional[int] = None
    entry_ts: Optional[pd.Timestamp] = None
    exit_idx: Optional[int] = None
    exit_ts: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    outcome: str = "not_triggered"  # "tp" | "stop" | "timeout" | "not_triggered"
    pnl_r: float = 0.0
    bars_held: int = 0


@dataclass
class BacktestResult:
    coin: str
    timeframe: str
    n_bars: int
    n_signals: int
    n_trades: int  # triggered (entered) trades
    n_wins: int
    n_losses: int
    n_timeouts: int
    win_rate: float
    avg_pnl_r: float
    expectancy_r: float
    cumulative_r: float
    max_drawdown_r: float
    by_entry_type: dict[str, dict[str, float]] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-bar summary
# ---------------------------------------------------------------------------


def _summary_at_bar(
    df: pd.DataFrame,
    bundle: IndicatorBundle,
    i: int,
) -> Optional[dict]:
    """Build the same dict shape as `IndicatorBundle.summary()` but for bar `i`.

    No look-ahead: every series is sliced to `[: i + 1]` and support /
    resistance pivots are recomputed only on bars up to `i`.
    """
    if i < WARMUP_BARS or i >= len(df):
        return None
    sliced = IndicatorBundle(
        df=df.iloc[: i + 1],
        ema_fast=bundle.ema_fast.iloc[: i + 1],
        ema_slow=bundle.ema_slow.iloc[: i + 1],
        ema_long=bundle.ema_long.iloc[: i + 1],
        rsi=bundle.rsi.iloc[: i + 1],
        macd=bundle.macd.iloc[: i + 1],
        bb=bundle.bb.iloc[: i + 1],
        atr=bundle.atr.iloc[: i + 1],
        adx=bundle.adx.iloc[: i + 1],
    )
    last_close = float(df["close"].iloc[i])
    atr_v_raw = bundle.atr.iloc[i]
    atr_v = float(atr_v_raw) if not math.isnan(float(atr_v_raw)) else last_close * 0.01
    res_raw, sup_raw = find_pivots(df.iloc[: i + 1], left=5, right=5)
    resistance = [
        p for p in cluster_levels(res_raw, last_close, atr_v, max_levels=6) if p >= last_close
    ]
    support = [
        p for p in cluster_levels(sup_raw, last_close, atr_v, max_levels=6) if p <= last_close
    ]
    sliced.support = sorted(set(support))[:4]
    sliced.resistance = sorted(set(resistance))[:4]
    return sliced.summary()


# ---------------------------------------------------------------------------
# Trade simulation primitives
# ---------------------------------------------------------------------------


def _try_trigger(
    df: pd.DataFrame,
    signal_idx: int,
    direction: str,
    entry_type: str,
    entry: float,
    max_wait: int,
) -> Optional[int]:
    """Find the first bar in (signal_idx, signal_idx + max_wait] where the
    entry order would have filled.

    For market orders the trigger is unconditional on the next bar.

    Returns the bar index, or None if it never triggered within the window.
    """
    if entry_type == "market":
        nxt = signal_idx + 1
        if nxt >= len(df):
            return None
        return nxt
    end = min(signal_idx + 1 + max_wait, len(df))
    for j in range(signal_idx + 1, end):
        bar = df.iloc[j]
        low = float(bar["low"])
        high = float(bar["high"])
        if direction == "long":
            if entry_type == "limit" and low <= entry:
                return j
            if entry_type == "stop" and high >= entry:
                return j
        else:  # short
            if entry_type == "limit" and high >= entry:
                return j
            if entry_type == "stop" and low <= entry:
                return j
    return None


def _walk_to_exit(
    df: pd.DataFrame,
    start_idx: int,
    direction: str,
    entry: float,
    stop: float,
    take_profit: float,
    max_hold: int,
) -> dict:
    """Walk forward from `start_idx` (inclusive) checking SL / TP / timeout.

    Conservative when SL and TP both fall inside the same bar's range — we
    assume SL hits first, since we cannot know intra-bar ordering and a real
    risk manager should not over-claim wins.
    """
    end_idx = min(start_idx + max_hold, len(df) - 1)
    for j in range(start_idx, end_idx + 1):
        bar = df.iloc[j]
        low = float(bar["low"])
        high = float(bar["high"])
        if direction == "long":
            sl_hit = low <= stop
            tp_hit = high >= take_profit
            if sl_hit:
                return {"outcome": "stop", "exit_idx": j, "exit_price": stop}
            if tp_hit:
                return {"outcome": "tp", "exit_idx": j, "exit_price": take_profit}
        else:
            sl_hit = high >= stop
            tp_hit = low <= take_profit
            if sl_hit:
                return {"outcome": "stop", "exit_idx": j, "exit_price": stop}
            if tp_hit:
                return {"outcome": "tp", "exit_idx": j, "exit_price": take_profit}
    return {
        "outcome": "timeout",
        "exit_idx": end_idx,
        "exit_price": float(df.iloc[end_idx]["close"]),
    }


def _pnl_r(direction: str, entry: float, stop: float, exit_price: float) -> float:
    """P&L in R-multiples.

    R = |entry - stop| (the trade's risk unit). One full SL hit = -1R.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    if direction == "long":
        return (exit_price - entry) / risk
    return (entry - exit_price) / risk


def simulate_trade(
    df: pd.DataFrame,
    signal_idx: int,
    signal: Signal,
    *,
    max_wait: int = DEFAULT_MAX_WAIT,
    max_hold: int = DEFAULT_MAX_HOLD,
) -> Optional[Trade]:
    """Simulate the lifetime of one signal — trigger, hold, exit.

    Returns `None` when the signal is unactionable (flat, missing levels).
    Returns a `Trade` with `triggered=False` if a limit/stop order never
    filled within `max_wait` bars.
    """
    direction = signal.direction
    if direction == "flat":
        return None
    entry = signal.entry
    stop = signal.stop_loss
    tp = signal.take_profit_1
    if entry is None or stop is None or tp is None:
        return None
    entry_type = signal.entry_type or "market"

    trade = Trade(
        direction=direction,
        entry_type=entry_type,
        signal_idx=signal_idx,
        signal_ts=df.index[signal_idx],
        planned_entry=float(entry),
        stop_loss=float(stop),
        take_profit=float(tp),
        confidence=int(signal.confidence),
        rr_planned=signal.rr,
        triggered=False,
    )

    trigger_idx = _try_trigger(df, signal_idx, direction, entry_type, float(entry), max_wait)
    if trigger_idx is None:
        return trade

    trade.triggered = True
    trade.entry_idx = trigger_idx
    trade.entry_ts = df.index[trigger_idx]

    exit_info = _walk_to_exit(
        df,
        trigger_idx,
        direction,
        float(entry),
        float(stop),
        float(tp),
        max_hold,
    )
    trade.exit_idx = exit_info["exit_idx"]
    trade.exit_ts = df.index[exit_info["exit_idx"]]
    trade.exit_price = float(exit_info["exit_price"])
    trade.outcome = exit_info["outcome"]
    trade.pnl_r = _pnl_r(direction, float(entry), float(stop), trade.exit_price)
    trade.bars_held = trade.exit_idx - trigger_idx
    return trade


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------


def _max_drawdown(cumulative: list[float]) -> float:
    """Largest peak-to-trough drop on a cumulative R curve. Returned as a
    positive number (i.e., 3.0 means the strategy gave back 3R from peak)."""
    peak = -math.inf
    max_dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _aggregate(trades: list[Trade]) -> dict[str, float]:
    triggered = [t for t in trades if t.triggered]
    n = len(triggered)
    wins = [t for t in triggered if t.outcome == "tp"]
    losses = [t for t in triggered if t.outcome == "stop"]
    timeouts = [t for t in triggered if t.outcome == "timeout"]
    pnl_list = [t.pnl_r for t in triggered]
    cumulative: list[float] = []
    running = 0.0
    for v in pnl_list:
        running += v
        cumulative.append(running)
    win_rate = (len(wins) / n) if n else 0.0
    avg = (sum(pnl_list) / n) if n else 0.0
    return {
        "n_triggered": n,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_timeouts": len(timeouts),
        "win_rate": win_rate,
        "avg_pnl_r": avg,
        "expectancy_r": avg,  # avg per trade R is by definition expectancy in R-terms
        "cumulative_r": running,
        "max_drawdown_r": _max_drawdown(cumulative),
    }


def run_backtest(
    df: pd.DataFrame,
    coin: str,
    timeframe: str,
    *,
    warmup: int = WARMUP_BARS,
    max_wait: int = DEFAULT_MAX_WAIT,
    max_hold: int = DEFAULT_MAX_HOLD,
    one_at_a_time: bool = True,
) -> BacktestResult:
    """Replay the rules-based engine bar by bar.

    `one_at_a_time` (default True): once a trade is triggered, ignore further
    signals until it has exited. Mirrors a single-position discretionary
    trader — without this we'd compound overlapping trades and the cumulative
    R curve becomes meaningless.
    """
    if len(df) <= warmup + 5:
        raise ValueError(
            f"Need at least {warmup + 6} bars; got {len(df)}. Try a longer history or smaller warmup."
        )
    bundle = compute_all(df)
    n_bars = len(df)
    n_signals = 0
    trades: list[Trade] = []
    blocked_until: int = -1  # exclusive index until which we ignore new signals

    for i in range(warmup, n_bars - 1):
        if one_at_a_time and i <= blocked_until:
            continue
        summary = _summary_at_bar(df, bundle, i)
        if summary is None:
            continue
        analysis = _rules_based_fallback(coin, timeframe, summary, extra=None)
        sig = analysis.signal
        if sig.direction == "flat":
            continue
        n_signals += 1
        trade = simulate_trade(df, i, sig, max_wait=max_wait, max_hold=max_hold)
        if trade is None:
            continue
        trades.append(trade)
        if trade.triggered and trade.exit_idx is not None and one_at_a_time:
            blocked_until = trade.exit_idx

    agg = _aggregate(trades)

    # Per-entry-type breakdown, useful for spotting "limit entries underperform".
    by_type: dict[str, dict[str, float]] = {}
    for et in ("market", "limit", "stop"):
        subset = [t for t in trades if t.entry_type == et]
        if subset:
            by_type[et] = _aggregate(subset)

    return BacktestResult(
        coin=coin,
        timeframe=timeframe,
        n_bars=n_bars,
        n_signals=n_signals,
        n_trades=int(agg["n_triggered"]),
        n_wins=int(agg["n_wins"]),
        n_losses=int(agg["n_losses"]),
        n_timeouts=int(agg["n_timeouts"]),
        win_rate=float(agg["win_rate"]),
        avg_pnl_r=float(agg["avg_pnl_r"]),
        expectancy_r=float(agg["expectancy_r"]),
        cumulative_r=float(agg["cumulative_r"]),
        max_drawdown_r=float(agg["max_drawdown_r"]),
        by_entry_type=by_type,
        trades=trades,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _trade_to_jsonable(t: Trade) -> dict:
    d = asdict(t)
    for k in ("signal_ts", "entry_ts", "exit_ts"):
        v = d.get(k)
        d[k] = v.isoformat() if isinstance(v, pd.Timestamp) else (str(v) if v is not None else None)
    return d


def _result_to_jsonable(res: BacktestResult, *, include_trades: bool) -> dict:
    payload = {
        "coin": res.coin,
        "timeframe": res.timeframe,
        "n_bars": res.n_bars,
        "n_signals": res.n_signals,
        "n_trades": res.n_trades,
        "n_wins": res.n_wins,
        "n_losses": res.n_losses,
        "n_timeouts": res.n_timeouts,
        "win_rate": round(res.win_rate, 4),
        "avg_pnl_r": round(res.avg_pnl_r, 4),
        "expectancy_r": round(res.expectancy_r, 4),
        "cumulative_r": round(res.cumulative_r, 4),
        "max_drawdown_r": round(res.max_drawdown_r, 4),
        "by_entry_type": {
            et: {k: round(v, 4) if isinstance(v, float) else v for k, v in stats.items()}
            for et, stats in res.by_entry_type.items()
        },
    }
    if include_trades:
        payload["trades"] = [_trade_to_jsonable(t) for t in res.trades]
    return payload


def _format_summary(res: BacktestResult) -> str:
    lines = [
        f"Backtest {res.coin}/{res.timeframe} — {res.n_bars} bars",
        f"  Signals generated:    {res.n_signals}",
        f"  Triggered trades:     {res.n_trades}",
        f"  TP / SL / timeouts:   {res.n_wins} / {res.n_losses} / {res.n_timeouts}",
        f"  Win rate:             {res.win_rate * 100:.1f}%",
        f"  Avg P&L per trade:    {res.avg_pnl_r:+.3f}R",
        f"  Cumulative P&L:       {res.cumulative_r:+.2f}R",
        f"  Max drawdown:         {res.max_drawdown_r:.2f}R",
    ]
    if res.by_entry_type:
        lines.append("  By entry_type:")
        for et, stats in res.by_entry_type.items():
            lines.append(
                f"    {et:6s}  trades={int(stats['n_triggered']):3d}  "
                f"win={stats['win_rate'] * 100:5.1f}%  "
                f"avgR={stats['avg_pnl_r']:+.3f}  "
                f"cumR={stats['cumulative_r']:+.2f}"
            )
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.backtest",
        description="Backtest the rules-based strategy on historical OHLCV.",
    )
    p.add_argument("--coin", default="BTC", help="Coin code (e.g. BTC, ETH). Default: BTC.")
    p.add_argument(
        "--timeframe", "--tf", dest="timeframe", default="1h",
        help="Timeframe (15m / 1h / 4h / 1d / 1w). Default: 1h.",
    )
    p.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT, help="Bars to wait for a limit/stop fill.")
    p.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD, help="Bars to hold before exit at market.")
    p.add_argument("--warmup", type=int, default=WARMUP_BARS, help="Bars to skip at the start for indicator warmup.")
    p.add_argument("--json", action="store_true", help="Print full JSON instead of a human summary.")
    p.add_argument("--trades", action="store_true", help="Include per-trade detail in JSON output.")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    df = fetch_ohlcv(args.coin, args.timeframe)
    res = run_backtest(
        df,
        args.coin,
        args.timeframe,
        warmup=args.warmup,
        max_wait=args.max_wait,
        max_hold=args.max_hold,
    )
    if args.json:
        print(json.dumps(_result_to_jsonable(res, include_trades=args.trades), indent=2, ensure_ascii=False))
    else:
        print(_format_summary(res))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

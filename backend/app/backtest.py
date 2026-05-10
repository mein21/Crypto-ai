"""Walk-forward backtest of the rules-based signal logic.

Replays the rules-based signal generation across historical bars on the same
timeframe the user is analysing, then simulates each trade with ATR-based
stop / take levels and walks forward bar-by-bar. Produces aggregate strategy
stats (win rate, profit factor, expectancy in ATR units) plus a window-by-
window breakdown so the user can see if the recent regime is friendlier than
the older one.

Why rules-only? Calling an LLM for every historical bar would be slow and
non-deterministic; the rules logic is fast and stable, and gives a useful
"how does our automatic plan behave" diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .indicators import (
    atr as atr_series,
    bollinger,
    cluster_levels,
    ema,
    find_pivots,
    macd as macd_calc,
    rsi as rsi_series,
)


@dataclass
class _Trade:
    direction: str  # "long" / "short"
    entry: float
    stop: float
    tp: float
    entry_idx: int
    rr: float  # take / risk in ATR units
    outcome: str = "open"  # "win" / "loss" / "open"
    bars_held: int = 0


def _rules_signal(close: float, last: dict) -> tuple[str, float, float, float]:
    """Return (direction, entry, stop_distance_atr, tp_distance_atr).

    Mirrors the high-level rules-based decision from `_rules_based_fallback`
    but works in ATR units so we can simulate without exact S/R lists.
    """
    trend = last["trend"]
    macd_state = last["macd_state"]
    rsi_v = last["rsi"]
    atr_v = last["atr"]
    if atr_v <= 0:
        return "flat", 0.0, 0.0, 0.0
    bullish = trend == "восходящий" and macd_state in {"бычий", "нейтрально"} and rsi_v < 70
    bearish = trend == "нисходящий" and macd_state in {"медвежий", "нейтрально"} and rsi_v > 30
    if bullish:
        return "long", float(close), 1.0 * atr_v, 1.7 * atr_v
    if bearish:
        return "short", float(close), 1.0 * atr_v, 1.7 * atr_v
    return "flat", 0.0, 0.0, 0.0


def _bar_summary(df: pd.DataFrame, i: int) -> dict | None:
    """Build a tiny indicator summary at row position `i` using only data up to `i`."""
    if i < 60:
        return None
    sub = df.iloc[: i + 1]
    close = sub["close"]
    ema_f = float(ema(close, 20).iloc[-1])
    ema_s = float(ema(close, 50).iloc[-1])
    ema_l = float(ema(close, 200).iloc[-1]) if len(close) >= 200 else float(ema(close, min(len(close), 100)).iloc[-1])
    rsi_v = float(rsi_series(close, 14).iloc[-1])
    macd_df = macd_calc(close)
    macd_v = float(macd_df["macd"].iloc[-1])
    sig_v = float(macd_df["signal"].iloc[-1])
    hist_v = float(macd_df["hist"].iloc[-1])
    atr_v = float(atr_series(sub, 14).iloc[-1])
    last_close = float(close.iloc[-1])

    if last_close > ema_l and ema_f > ema_s:
        trend = "восходящий"
    elif last_close < ema_l and ema_f < ema_s:
        trend = "нисходящий"
    else:
        trend = "боковой"
    macd_state = (
        "бычий" if hist_v > 0 and macd_v > sig_v else "медвежий" if hist_v < 0 and macd_v < sig_v else "нейтрально"
    )
    return {
        "trend": trend,
        "macd_state": macd_state,
        "rsi": rsi_v,
        "atr": atr_v,
    }


def _resolve_trade(df: pd.DataFrame, t: _Trade, max_hold: int = 30) -> _Trade:
    """Walk forward and resolve a trade against TP / SL using OHLC."""
    n = len(df)
    end = min(n - 1, t.entry_idx + max_hold)
    for j in range(t.entry_idx + 1, end + 1):
        high = float(df["high"].iloc[j])
        low = float(df["low"].iloc[j])
        if t.direction == "long":
            if low <= t.stop:
                t.outcome = "loss"
                t.bars_held = j - t.entry_idx
                return t
            if high >= t.tp:
                t.outcome = "win"
                t.bars_held = j - t.entry_idx
                return t
        else:
            if high >= t.stop:
                t.outcome = "loss"
                t.bars_held = j - t.entry_idx
                return t
            if low <= t.tp:
                t.outcome = "win"
                t.bars_held = j - t.entry_idx
                return t
    # Closed by time-stop at the end of the window
    last_close = float(df["close"].iloc[end])
    pnl = (last_close - t.entry) if t.direction == "long" else (t.entry - last_close)
    t.bars_held = end - t.entry_idx
    t.outcome = "win" if pnl > 0 else "loss"
    return t


def _aggregate_trades(trades: list[_Trade]) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_rr": 0.0,
            "profit_factor": 0.0,
            "expectancy_atr": 0.0,
            "longs": 0,
            "shorts": 0,
        }
    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]
    win_rate = len(wins) / len(trades) * 100.0
    # Profit factor on ATR-units: sum(rr on wins) / sum(1 on losses).
    gross_win = sum(t.rr for t in wins)
    gross_loss = float(len(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    # Expectancy in ATR units: P(win)*rr - P(loss)*1
    p_win = len(wins) / len(trades)
    p_loss = len(losses) / len(trades)
    avg_rr = (gross_win / len(wins)) if wins else 0.0
    expectancy = p_win * avg_rr - p_loss * 1.0
    return {
        "total_trades": int(len(trades)),
        "win_rate": round(float(win_rate), 1),
        "avg_rr": round(float(avg_rr), 2),
        "profit_factor": round(float(pf), 2) if pf != float("inf") else 99.99,
        "expectancy_atr": round(float(expectancy), 3),
        "longs": int(sum(1 for t in trades if t.direction == "long")),
        "shorts": int(sum(1 for t in trades if t.direction == "short")),
    }


def walk_forward_backtest(
    df: pd.DataFrame,
    *,
    lookback_bars: int = 200,
    n_windows: int = 4,
    max_hold: int = 30,
) -> dict | None:
    """Run a walk-forward backtest over the last `lookback_bars` rows.

    Splits that window into `n_windows` equal chunks for window-by-window
    statistics so the UI can show "does it work in the recent regime?".
    """
    if df.empty or len(df) < 80:
        return None
    n = len(df)
    start = max(60, n - lookback_bars)
    chunk_size = max(20, (n - start) // max(1, n_windows))

    trades: list[_Trade] = []
    last_open: _Trade | None = None
    for i in range(start, n - 1):
        if last_open is not None:
            # Don't open a new trade while one is alive.
            continue
        s = _bar_summary(df, i)
        if s is None:
            continue
        close = float(df["close"].iloc[i])
        direction, entry, stop_d, tp_d = _rules_signal(close, s)
        if direction == "flat":
            continue
        stop = entry - stop_d if direction == "long" else entry + stop_d
        tp = entry + tp_d if direction == "long" else entry - tp_d
        rr = (tp_d / stop_d) if stop_d > 0 else 0.0
        t = _Trade(
            direction=direction,
            entry=entry,
            stop=stop,
            tp=tp,
            entry_idx=i,
            rr=rr,
        )
        last_open = t
        resolved = _resolve_trade(df, t, max_hold=max_hold)
        trades.append(resolved)
        last_open = None

    overall = _aggregate_trades(trades)

    # Window-by-window
    windows = []
    for w in range(n_windows):
        w_start = start + w * chunk_size
        w_end = w_start + chunk_size if w < n_windows - 1 else n - 1
        in_window = [t for t in trades if w_start <= t.entry_idx < w_end]
        stats = _aggregate_trades(in_window)
        stats["window"] = int(w + 1)
        stats["bars"] = int(w_end - w_start)
        windows.append(stats)
    overall["windows"] = windows
    overall["lookback_bars"] = int(n - start)
    return overall


def backtest_summary_for_prompt(stats: dict | None) -> str:
    if not stats or stats.get("total_trades", 0) == 0:
        return ""
    return (
        f"  • Walk-forward: {stats['total_trades']} сделок · winrate {stats['win_rate']}% · "
        f"PF {stats['profit_factor']} · expectancy {stats['expectancy_atr']} ATR "
        f"(окно {stats['lookback_bars']} баров)"
    )


__all__ = [
    "walk_forward_backtest",
    "backtest_summary_for_prompt",
]


# Re-export utilities other modules may need (avoids unused-import warning above)
_ = (cluster_levels, find_pivots, bollinger, np)

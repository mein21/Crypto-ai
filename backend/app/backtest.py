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
    adx as adx_series,
    atr as atr_series,
    bollinger,
    cluster_levels,
    ema,
    find_pivots,
    macd as macd_calc,
    rsi as rsi_series,
)

# Tunables — tweaked together when calibrating the rules-based fallback.
ADX_GATE = 20.0   # below this we treat the regime as боковик / chop
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0   # was 1.7 — lifts theoretical break-even WR from 37% → 33%
RSI_LONG_MAX = 65.0   # was implicitly 70 — avoid late-trend longs
RSI_SHORT_MIN = 35.0  # symmetric for shorts


@dataclass
class _Trade:
    direction: str  # "long" / "short"
    entry: float
    stop: float
    tp: float
    entry_idx: int
    rr: float  # configured take / risk in ATR units (RR target at the time of entry)
    risk_atr: float = 0.0  # |entry - stop| (1 R in price units)
    realised_r: float = 0.0  # signed R actually achieved at exit
    outcome: str = "open"  # "win" / "loss" / "open"
    exit_kind: str = "open"  # "tp" / "sl" / "ema_cross" / "time_stop"
    bars_held: int = 0


def _rules_signal(close: float, last: dict) -> tuple[str, float, float, float]:
    """Return (direction, entry, stop_distance_atr, tp_distance_atr).

    Mirrors the high-level rules-based decision but works in ATR units.
    Filters: trend (EMA stack) + MACD agreement + RSI not over-extended
    + ADX above the chop gate (no entries in sideways regimes).
    """
    trend = last["trend"]
    macd_state = last["macd_state"]
    rsi_v = last["rsi"]
    atr_v = last["atr"]
    adx_v = float(last.get("adx", 0.0) or 0.0)
    if atr_v <= 0:
        return "flat", 0.0, 0.0, 0.0
    if adx_v < ADX_GATE:
        return "flat", 0.0, 0.0, 0.0
    bullish = (
        trend == "восходящий"
        and macd_state in {"бычий", "нейтрально"}
        and rsi_v < RSI_LONG_MAX
    )
    bearish = (
        trend == "нисходящий"
        and macd_state in {"медвежий", "нейтрально"}
        and rsi_v > RSI_SHORT_MIN
    )
    if bullish:
        return "long", float(close), SL_ATR_MULT * atr_v, TP_ATR_MULT * atr_v
    if bearish:
        return "short", float(close), SL_ATR_MULT * atr_v, TP_ATR_MULT * atr_v
    return "flat", 0.0, 0.0, 0.0


def _precompute(df: pd.DataFrame) -> dict:
    """Compute all indicator series once per backtest call (avoids O(n²) recompute)."""
    close = df["close"]
    ema_f = ema(close, 20)
    ema_s = ema(close, 50)
    ema_l = ema(close, 200) if len(close) >= 200 else ema(close, min(len(close), 100))
    rsi_v = rsi_series(close, 14)
    macd_df = macd_calc(close)
    atr_v = atr_series(df, 14)
    try:
        adx_v = adx_series(df, 14)
    except Exception:  # noqa: BLE001 — fall back gracefully if ADX cannot be computed
        adx_v = pd.Series(np.zeros(len(df), dtype=float), index=df.index)
    return {
        "close": close,
        "ema_f": ema_f,
        "ema_s": ema_s,
        "ema_l": ema_l,
        "rsi": rsi_v,
        "macd": macd_df["macd"],
        "macd_signal": macd_df["signal"],
        "macd_hist": macd_df["hist"],
        "atr": atr_v,
        "adx": adx_v,
    }


def _bar_summary(pre: dict, i: int) -> dict | None:
    """Build the indicator snapshot at row position `i` from the pre-computed series."""
    if i < 60:
        return None
    last_close = float(pre["close"].iloc[i])
    ema_f = float(pre["ema_f"].iloc[i])
    ema_s = float(pre["ema_s"].iloc[i])
    ema_l = float(pre["ema_l"].iloc[i])
    rsi_v = float(pre["rsi"].iloc[i])
    macd_v = float(pre["macd"].iloc[i])
    sig_v = float(pre["macd_signal"].iloc[i])
    hist_v = float(pre["macd_hist"].iloc[i])
    atr_v = float(pre["atr"].iloc[i])
    adx_v = float(pre["adx"].iloc[i]) if not np.isnan(pre["adx"].iloc[i]) else 0.0

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
        "adx": adx_v,
    }


def _close_at_price(t: _Trade, exit_price: float, kind: str, j: int) -> _Trade:
    pnl = (exit_price - t.entry) if t.direction == "long" else (t.entry - exit_price)
    realised = (pnl / t.risk_atr) if t.risk_atr > 0 else 0.0
    t.realised_r = float(realised)
    t.exit_kind = kind
    t.bars_held = j - t.entry_idx
    t.outcome = "win" if realised > 0 else "loss"
    return t


def _resolve_trade(
    df: pd.DataFrame,
    pre: dict,
    t: _Trade,
    max_hold: int = 30,
) -> _Trade:
    """Walk forward and resolve a trade against TP / SL / EMA-cross / time-stop.

    EMA-cross exit: if the fast EMA crosses against our direction (EMA20<EMA50
    while long, EMA20>EMA50 while short), close at the bar's close. Captures
    early trend exhaustion before SL is touched and converts marginal winners
    into realised gains instead of round-trips back to SL.
    """
    n = len(df)
    end = min(n - 1, t.entry_idx + max_hold)
    high_s = df["high"]
    low_s = df["low"]
    close_s = df["close"]
    ema_f = pre["ema_f"]
    ema_s = pre["ema_s"]
    for j in range(t.entry_idx + 1, end + 1):
        high = float(high_s.iloc[j])
        low = float(low_s.iloc[j])
        if t.direction == "long":
            if low <= t.stop:
                return _close_at_price(t, t.stop, "sl", j)
            if high >= t.tp:
                return _close_at_price(t, t.tp, "tp", j)
            if float(ema_f.iloc[j]) < float(ema_s.iloc[j]):
                return _close_at_price(t, float(close_s.iloc[j]), "ema_cross", j)
        else:
            if high >= t.stop:
                return _close_at_price(t, t.stop, "sl", j)
            if low <= t.tp:
                return _close_at_price(t, t.tp, "tp", j)
            if float(ema_f.iloc[j]) > float(ema_s.iloc[j]):
                return _close_at_price(t, float(close_s.iloc[j]), "ema_cross", j)
    return _close_at_price(t, float(close_s.iloc[end]), "time_stop", end)


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
    # Use realised R per trade (signed). EMA-cross / time-stop wins earn
    # a fractional R rather than the full TP target, which is more honest
    # than crediting them at the configured RR.
    gross_win = sum(t.realised_r for t in wins)
    gross_loss = sum(-t.realised_r for t in losses)  # both summands positive
    pf = (
        (gross_win / gross_loss) if gross_loss > 0
        else float("inf") if gross_win > 0
        else 0.0
    )
    avg_rr = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 1.0
    p_win = len(wins) / len(trades)
    p_loss = len(losses) / len(trades)
    expectancy = p_win * avg_rr - p_loss * avg_loss
    return {
        "total_trades": int(len(trades)),
        "win_rate": round(float(win_rate), 1),
        "avg_rr": round(float(avg_rr), 2),
        "profit_factor": round(float(pf), 2) if pf != float("inf") else 99.99,
        "expectancy_atr": round(float(expectancy), 3),
        "longs": int(sum(1 for t in trades if t.direction == "long")),
        "shorts": int(sum(1 for t in trades if t.direction == "short")),
        "tp_hits": int(sum(1 for t in trades if t.exit_kind == "tp")),
        "sl_hits": int(sum(1 for t in trades if t.exit_kind == "sl")),
        "ema_exits": int(sum(1 for t in trades if t.exit_kind == "ema_cross")),
        "time_stops": int(sum(1 for t in trades if t.exit_kind == "time_stop")),
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
    pre = _precompute(df)

    trades: list[_Trade] = []
    last_open: _Trade | None = None
    for i in range(start, n - 1):
        if last_open is not None:
            # Don't open a new trade while one is alive.
            continue
        s = _bar_summary(pre, i)
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
            risk_atr=stop_d,
        )
        last_open = t
        resolved = _resolve_trade(df, pre, t, max_hold=max_hold)
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
    breakdown = ""
    tp = stats.get("tp_hits")
    sl = stats.get("sl_hits")
    em = stats.get("ema_exits")
    ts = stats.get("time_stops")
    if tp is not None:
        breakdown = f" [TP {tp}/SL {sl}/EMA {em}/time {ts}]"
    return (
        f"  • Walk-forward: {stats['total_trades']} сделок · winrate {stats['win_rate']}% · "
        f"PF {stats['profit_factor']} · expectancy {stats['expectancy_atr']} ATR "
        f"(окно {stats['lookback_bars']} баров){breakdown}"
    )


__all__ = [
    "walk_forward_backtest",
    "backtest_summary_for_prompt",
]


# Re-export utilities other modules may need (avoids unused-import warning above)
_ = (cluster_levels, find_pivots, bollinger, np)

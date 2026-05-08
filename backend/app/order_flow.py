"""Approximate order flow from OHLCV (no L2 / trade-tape access).

For each candle we approximate the share of "buying" and "selling" volume by
where the close lands inside the bar:

    bull_share = (close - low) / (high - low)
    bear_share = (high - close) / (high - low)
    delta = volume * (bull_share - bear_share)

The cumulative sum of delta is the **CVD** (Cumulative Volume Delta). This is
a textbook proxy used when a trader has no direct access to bid/ask trades.

We then look for **CVD ↔ price divergences**: price prints a higher high while
CVD prints a lower high (bearish), or price prints a lower low while CVD prints
a higher low (bullish).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class OrderFlowBundle:
    delta: pd.Series
    cvd: pd.Series
    last_value: float
    slope: float
    buy_pressure_pct: float
    divergence: str  # "bullish" | "bearish" | "none"
    divergence_note: str

    def to_summary(self) -> dict:
        return {
            "cvd_value": round(float(self.last_value), 4),
            "cvd_slope": round(float(self.slope), 6),
            "buy_pressure_pct": round(float(self.buy_pressure_pct), 2),
            "divergence": self.divergence,
            "divergence_note": self.divergence_note,
        }


def _candle_delta(df: pd.DataFrame) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    rng = (high - low).replace(0, np.nan)
    bull = (close - low) / rng
    bear = (high - close) / rng
    delta = (df["volume"].astype(float) * (bull - bear)).fillna(0.0)
    return delta


def _detect_divergence(price: pd.Series, cvd: pd.Series, lookback: int = 20) -> tuple[str, str]:
    """Detect a recent CVD / price divergence in the last `lookback` bars."""
    if len(price) < lookback or len(cvd) < lookback:
        return "none", ""
    p = price.tail(lookback).values
    c = cvd.tail(lookback).values

    # Compare the last bar's value with the max/min of the prior portion.
    half = max(3, lookback // 2)
    prior_p = p[:-half]
    prior_c = c[:-half]
    recent_p = p[-half:]
    recent_c = c[-half:]
    if prior_p.size == 0 or recent_p.size == 0:
        return "none", ""

    prior_p_max = float(prior_p.max())
    prior_p_min = float(prior_p.min())
    prior_c_max = float(prior_c.max())
    prior_c_min = float(prior_c.min())
    recent_p_max = float(recent_p.max())
    recent_p_min = float(recent_p.min())
    recent_c_max = float(recent_c.max())
    recent_c_min = float(recent_c.min())

    # Bearish: price made a higher high but CVD made a lower high.
    if recent_p_max > prior_p_max * 1.001 and recent_c_max < prior_c_max:
        return (
            "bearish",
            "цена обновила максимум, CVD — нет: продавцы не дают объёма на росте",
        )
    # Bullish: price made a lower low but CVD made a higher low.
    if recent_p_min < prior_p_min * 0.999 and recent_c_min > prior_c_min:
        return (
            "bullish",
            "цена обновила минимум, CVD — нет: покупатели абсорбируют слив",
        )
    return "none", ""


def compute_order_flow(df: pd.DataFrame, *, divergence_lookback: int = 20) -> OrderFlowBundle | None:
    if df.empty or len(df) < 10:
        return None
    delta = _candle_delta(df)
    cvd = delta.cumsum()
    last_value = float(cvd.iloc[-1])
    slope_window = min(10, len(cvd))
    if slope_window >= 2:
        y = cvd.tail(slope_window).values.astype(float)
        x = np.arange(slope_window, dtype=float)
        # simple slope (rise / run); robust enough for trend direction
        slope = float((y[-1] - y[0]) / max(slope_window - 1, 1))
    else:
        slope = 0.0

    pressure_window = df.tail(20)
    pressure_total = float(pressure_window["volume"].sum())
    if pressure_total > 0:
        bull = (
            pressure_window["volume"].astype(float)
            * ((pressure_window["close"].astype(float) - pressure_window["low"].astype(float))
               / (pressure_window["high"].astype(float) - pressure_window["low"].astype(float)).replace(0, np.nan))
        ).fillna(0.0).sum()
        buy_pressure_pct = float(bull) / pressure_total * 100.0
    else:
        buy_pressure_pct = 50.0

    divergence, note = _detect_divergence(df["close"].astype(float), cvd, lookback=divergence_lookback)

    return OrderFlowBundle(
        delta=delta,
        cvd=cvd,
        last_value=last_value,
        slope=slope,
        buy_pressure_pct=buy_pressure_pct,
        divergence=divergence,
        divergence_note=note,
    )


def order_flow_summary_for_prompt(flow: dict | None) -> str:
    if not flow:
        return ""
    parts = [
        f"  • CVD={flow['cvd_value']}, наклон={flow['cvd_slope']}, давление покупок {flow['buy_pressure_pct']}%"
    ]
    div = flow.get("divergence", "none")
    if div != "none":
        parts.append(f"  • дивергенция CVD: {div} — {flow.get('divergence_note', '')}")
    return "\n".join(parts)

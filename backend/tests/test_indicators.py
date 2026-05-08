"""Unit tests for indicator math added by the strategy review."""
from __future__ import annotations

import pandas as pd
import pytest

from app.indicators import (
    _bb_width_state,
    _classify_macd,
    adx,
    compute_all,
    rsi,
)


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="1h", tz="UTC")
    return pd.Series(values, index=idx, dtype=float)


def _ohlc_from_close(values: list[float]) -> pd.DataFrame:
    """Build an OHLCV frame from a close series (open=prev close, body fills)."""
    idx = pd.date_range("2024-01-01", periods=len(values), freq="1h", tz="UTC")
    closes = pd.Series(values, index=idx, dtype=float)
    opens = closes.shift(1).fillna(closes)
    highs = pd.concat([opens, closes], axis=1).max(axis=1) + 0.5
    lows = pd.concat([opens, closes], axis=1).min(axis=1) - 0.5
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1.0] * len(values),
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# RSI edge cases (B1)
# ---------------------------------------------------------------------------

def test_rsi_only_gains_returns_100():
    """If avg_loss is zero and avg_gain is positive, RSI must be 100, not 50."""
    s = _series([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115])
    out = rsi(s, length=14)
    assert out.iloc[-1] == pytest.approx(100.0)


def test_rsi_only_losses_returns_0():
    s = _series([115, 114, 113, 112, 111, 110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100])
    out = rsi(s, length=14)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_series_returns_50():
    s = _series([100.0] * 20)
    out = rsi(s, length=14)
    # When both gains and losses are zero RSI is undefined — we contract on 50.
    assert out.iloc[-1] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# MACD six-bucket classifier (B2)
# ---------------------------------------------------------------------------

def _macd_df(macd_v: float, sig_v: float, hist_now: float, hist_prev: float) -> pd.DataFrame:
    """Build a tiny 3-row MACD frame with the wanted last-two hist values."""
    return pd.DataFrame(
        {
            "macd": [macd_v - 1.0, macd_v - 0.5, macd_v],
            "signal": [sig_v, sig_v, sig_v],
            "hist": [0.0, hist_prev, hist_now],
        }
    )


def test_macd_classifier_buckets():
    """Each bucket corresponds to a (macd vs signal, hist trend) combination."""
    # bullish-acceleration: macd > signal AND hist growing
    assert _classify_macd(_macd_df(0.5, 0.2, 0.3, 0.1)) == "бычий-разгон"
    # bullish: macd > signal but hist decelerating
    assert _classify_macd(_macd_df(0.5, 0.2, 0.1, 0.3)) == "бычий"
    # reversal-up: hist just crossed positive
    assert _classify_macd(_macd_df(0.1, 0.2, 0.05, -0.05)) == "разворот вверх"
    # bearish-acceleration: macd < signal AND hist growing more negative
    assert _classify_macd(_macd_df(-0.5, -0.2, -0.3, -0.1)) == "медвежий-разгон"
    # bearish: macd < signal, hist becoming less negative
    assert _classify_macd(_macd_df(-0.5, -0.2, -0.1, -0.3)) == "медвежий"
    # reversal-down: hist just crossed negative
    assert _classify_macd(_macd_df(0.2, 0.1, -0.05, 0.05)) == "разворот вниз"


# ---------------------------------------------------------------------------
# Bollinger width state (B3)
# ---------------------------------------------------------------------------

def _bb_with_widths(widths: list[float]) -> pd.DataFrame:
    """Synthetic BB frame where (upper - lower) / mid yields the wanted widths."""
    n = len(widths)
    mid = pd.Series([100.0] * n)
    w = pd.Series(widths)
    upper = mid * (1 + w / 2.0)
    lower = mid * (1 - w / 2.0)
    return pd.DataFrame({"bb_lower": lower, "bb_mid": mid, "bb_upper": upper})


def test_bb_width_state_squeeze_when_low():
    widths = [0.05] * 90 + [0.005] * 10
    _, state = _bb_width_state(_bb_with_widths(widths))
    assert state == "сжатие"


def test_bb_width_state_expansion_when_high():
    widths = [0.05] * 90 + [0.5] * 10
    _, state = _bb_width_state(_bb_with_widths(widths))
    assert state == "расширение"


def test_bb_width_state_normal_when_average():
    # 99 ascending values from 0.01..0.108, then 0.05 — median percentile.
    widths = [0.01 + 0.001 * i for i in range(99)] + [0.05]
    _, state = _bb_width_state(_bb_with_widths(widths))
    assert state == "нормальная"


# ---------------------------------------------------------------------------
# ADX (B4)
# ---------------------------------------------------------------------------

def test_adx_strong_uptrend_above_25():
    # Monotone climb — ADX should report a strong trend.
    df = _ohlc_from_close([100 + i * 0.5 for i in range(80)])
    out = adx(df, length=14)
    assert out.iloc[-1] >= 25, f"expected strong trend, got {out.iloc[-1]:.1f}"


def test_adx_choppy_market_low_relative_to_strong_trend():
    # Chop with no persistent direction — ADX should be much lower than
    # the steady-uptrend baseline. Compare relatively to make this robust
    # to how Wilder smoothing initialises on synthetic data.
    trend_df = _ohlc_from_close([100 + i * 0.5 for i in range(80)])
    rng = [100, 102, 99, 103, 98, 104, 97, 105, 99, 102] * 8
    chop_df = _ohlc_from_close(rng[:80])
    trend_adx = float(adx(trend_df, length=14).iloc[-1])
    chop_adx = float(adx(chop_df, length=14).iloc[-1])
    assert trend_adx > chop_adx, f"trend ADX {trend_adx:.1f} should exceed chop ADX {chop_adx:.1f}"


# ---------------------------------------------------------------------------
# IndicatorBundle.summary now exposes ADX, BB-state, ATR-pct
# ---------------------------------------------------------------------------

def test_summary_exposes_new_fields():
    closes = [100 + i * 0.4 for i in range(220)]
    df = _ohlc_from_close(closes)
    bundle = compute_all(df)
    summary = bundle.summary()
    for key in ("adx", "adx_state", "bb_state", "bb_width", "atr_pct", "macd_state"):
        assert key in summary, f"missing {key} in summary"
    assert summary["atr_pct"] >= 0
    assert summary["adx"] >= 0

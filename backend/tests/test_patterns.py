"""Unit tests for candlestick pattern detection."""
from __future__ import annotations

import pandas as pd
import pytest

from app.patterns import detect_patterns, patterns_summary_for_prompt


def _make_df(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Build a DataFrame from (open, high, low, close) tuples; volume = 1.0.

    The DatetimeIndex is hourly and tz-aware (UTC), matching what the
    upstream OHLCV fetcher provides.
    """
    idx = pd.date_range("2024-01-01", periods=len(bars), freq="1h", tz="UTC")
    return pd.DataFrame(
        [
            {"open": o, "high": h, "low": lo, "close": c, "volume": 1.0}
            for (o, h, lo, c) in bars
        ],
        index=idx,
    )


def _names(patterns: list[dict]) -> list[str]:
    return [p["name"] for p in patterns]


# ---------------------------------------------------------------------------
# Single-bar patterns
# ---------------------------------------------------------------------------

def test_doji_detected():
    """A flat-body candle following a downtrend is reported as Doji."""
    bars = [
        (100, 101, 99, 100),
        (100, 101, 98, 98),
        (98, 99, 96, 96),
        (96, 97, 94, 94),
        (94, 95, 92, 92),
        (92, 93, 90, 90),
        (90, 91, 88, 88),
        (88, 89, 86, 86),
        (86, 87, 84, 84),
        (84, 85, 83.5, 84),  # doji
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Doji" in _names(patterns)


def test_hammer_after_downtrend():
    bars = [
        (100, 101, 99, 100),
        (100, 100.5, 96, 96),
        (96, 96.5, 92, 92),
        (92, 92.5, 88, 88),
        (88, 88.5, 84, 84),
        (84, 84.5, 80, 80),
        (80, 80.5, 76, 76),
        (76, 76.5, 72, 72),
        (72, 72.5, 68, 68),
        (66, 68.5, 58, 68),  # long lower shadow, body in upper third
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    names = _names(patterns)
    assert "Hammer" in names


def test_shooting_star_after_uptrend():
    bars = [
        (60, 61, 59, 60),
        (60, 65, 60, 64),
        (64, 69, 64, 68),
        (68, 73, 68, 72),
        (72, 77, 72, 76),
        (76, 81, 76, 80),
        (80, 85, 80, 84),
        (84, 89, 84, 88),
        (88, 93, 88, 92),
        (93, 102, 92.9, 94),  # long upper wick, body in lower third
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Shooting Star" in _names(patterns)


def test_bullish_marubozu_detected():
    bars = [
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 100.5, 99.5, 100),
        (100, 105, 100, 105),  # full-body bullish candle
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Bullish Marubozu" in _names(patterns)


# ---------------------------------------------------------------------------
# Two-bar patterns
# ---------------------------------------------------------------------------

def test_bullish_engulfing_after_downtrend():
    bars = [
        (110, 111, 109, 110),
        (110, 111, 105, 105),
        (105, 106, 100, 100),
        (100, 101, 95, 95),
        (95, 96, 90, 90),
        (90, 91, 85, 85),
        (85, 86, 80, 80),
        (80, 81, 75, 75),
        (75, 76, 70, 71),  # small bearish bar
        (70, 78, 70, 77),  # bullish body engulfs prior body
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Bullish Engulfing" in _names(patterns)


def test_bearish_engulfing_after_uptrend():
    bars = [
        (60, 61, 59, 60),
        (60, 65, 60, 64),
        (64, 69, 64, 68),
        (68, 73, 68, 72),
        (72, 77, 72, 76),
        (76, 81, 76, 80),
        (80, 85, 80, 84),
        (84, 89, 84, 88),
        (88, 91, 88, 90),  # small bullish
        (91, 92, 84, 85),  # bearish body engulfs prior
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Bearish Engulfing" in _names(patterns)


# ---------------------------------------------------------------------------
# Three-bar patterns
# ---------------------------------------------------------------------------

def test_morning_star_detected():
    bars = [
        (110, 111, 109, 110),
        (110, 111, 105, 105),
        (105, 106, 100, 100),
        (100, 101, 95, 95),
        (95, 96, 90, 90),
        (90, 91, 85, 85),
        (85, 86, 80, 80),
        (80, 82, 70, 71),  # long bearish (a)
        (68, 69, 66, 68),  # small body, gapped down (b)
        (70, 80, 70, 79),  # strong bullish closing > a midpoint (c)
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Morning Star" in _names(patterns)


def test_evening_star_detected():
    bars = [
        (60, 61, 59, 60),
        (60, 65, 60, 64),
        (64, 69, 64, 68),
        (68, 73, 68, 72),
        (72, 77, 72, 76),
        (76, 81, 76, 80),
        (80, 85, 80, 84),
        (84, 96, 84, 95),  # long bullish (a)
        (97, 98, 96.5, 97.2),  # small body gapped up (b)
        (96, 96.5, 86, 87),  # strong bearish closing < a midpoint (c)
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Evening Star" in _names(patterns)


def test_three_white_soldiers():
    bars = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 105, 100, 104.5),
        (101, 109, 101, 108.5),
        (105, 113, 105, 112.5),
    ]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert "Three White Soldiers" in _names(patterns)


# ---------------------------------------------------------------------------
# Context scoring
# ---------------------------------------------------------------------------

def test_context_boosts_strength_at_support():
    """A Hammer right at a known support level gets strength = 3."""
    bars = [
        (100, 101, 99, 100),
        (100, 100.5, 96, 96),
        (96, 96.5, 92, 92),
        (92, 92.5, 88, 88),
        (88, 88.5, 84, 84),
        (84, 84.5, 80, 80),
        (80, 80.5, 76, 76),
        (76, 76.5, 72, 72),
        (72, 72.5, 68, 68),
        (66, 68.5, 58, 68),  # hammer with low ~58
    ]
    df = _make_df(bars)
    near = detect_patterns(
        df,
        lookback=3,
        support=[58.0],
        resistance=[100.0],
        atr=2.0,
        trend_label="нисходящий",
    )
    far = detect_patterns(df, lookback=3, atr=2.0)
    near_hammer = next((p for p in near if p["name"] == "Hammer"), None)
    far_hammer = next((p for p in far if p["name"] == "Hammer"), None)
    assert near_hammer is not None and far_hammer is not None
    assert near_hammer["strength"] >= far_hammer["strength"]
    assert near_hammer["strength"] == 3


# ---------------------------------------------------------------------------
# Public API contract
# ---------------------------------------------------------------------------

def test_detect_patterns_returns_empty_for_short_df():
    df = _make_df([(100, 101, 99, 100), (100, 101, 99, 100)])
    assert detect_patterns(df) == []


def test_detect_patterns_keys():
    bars = [(100, 101, 99, 100)] * 7 + [(100, 105, 100, 105)]
    df = _make_df(bars)
    patterns = detect_patterns(df, lookback=3)
    assert patterns, "expected at least one pattern hit"
    sample = patterns[0]
    expected_keys = {
        "name",
        "name_ru",
        "bias",
        "kind",
        "strength",
        "base_strength",
        "ts",
        "bar_index",
        "context",
    }
    assert expected_keys.issubset(sample.keys())
    assert 1 <= sample["strength"] <= 3
    assert sample["bias"] in {"bullish", "bearish", "neutral"}
    assert sample["kind"] in {"reversal", "continuation", "indecision"}
    assert sample["bar_index"] <= -1  # negative offset from end


def test_patterns_summary_for_prompt_is_human_readable():
    fake = [
        {
            "name": "Hammer",
            "name_ru": "Молот",
            "bias": "bullish",
            "kind": "reversal",
            "strength": 3,
            "base_strength": 2,
            "ts": 0,
            "bar_index": -1,
            "context": "у поддержки 60.00",
        }
    ]
    s = patterns_summary_for_prompt(fake)
    assert "Молот" in s
    assert "сила 3/3" in s


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

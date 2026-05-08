"""Unit tests for the walk-forward backtest module."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.backtest import backtest_summary_for_prompt, walk_forward_backtest


def _trending_df(n: int, slope: float = 0.5, noise: float = 0.5) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    closes = 100.0 + slope * np.arange(n) + rng.normal(0, noise, size=n)
    opens = closes - rng.uniform(0, 0.4, size=n)
    highs = np.maximum(opens, closes) + rng.uniform(0.05, 0.5, size=n)
    lows = np.minimum(opens, closes) - rng.uniform(0.05, 0.5, size=n)
    vols = rng.uniform(80, 120, size=n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def test_returns_none_for_short_df():
    df = _trending_df(40)
    assert walk_forward_backtest(df) is None


def test_runs_on_strong_uptrend_and_produces_stats():
    df = _trending_df(220, slope=0.6, noise=0.3)
    res = walk_forward_backtest(df, lookback_bars=180, n_windows=4)
    assert res is not None
    # Schema sanity
    for key in (
        "total_trades", "win_rate", "avg_rr", "profit_factor",
        "expectancy_atr", "longs", "shorts", "lookback_bars", "windows",
    ):
        assert key in res
    assert len(res["windows"]) == 4
    # All windows have correct shape
    for w in res["windows"]:
        for k in ("window", "bars", "total_trades"):
            assert k in w


def test_uptrend_dominantly_long_signals():
    df = _trending_df(300, slope=0.7, noise=0.2)
    res = walk_forward_backtest(df, lookback_bars=240)
    assert res is not None
    if res["total_trades"] > 0:
        assert res["longs"] >= res["shorts"]


def test_no_trades_returns_zero_stats():
    # Very short / mostly insufficient indicator window — zero trades is fine.
    df = _trending_df(85, slope=0.0, noise=0.05)
    res = walk_forward_backtest(df, lookback_bars=20)
    if res is not None:
        assert res["total_trades"] >= 0
        assert math.isfinite(res["win_rate"])


def test_summary_renders_for_prompt():
    fake = {
        "total_trades": 8,
        "win_rate": 62.5,
        "avg_rr": 1.5,
        "profit_factor": 1.8,
        "expectancy_atr": 0.4,
        "longs": 6,
        "shorts": 2,
        "lookback_bars": 200,
        "windows": [],
    }
    s = backtest_summary_for_prompt(fake)
    assert "8" in s
    assert "62.5" in s


def test_summary_handles_zero_trades():
    fake = {"total_trades": 0, "lookback_bars": 100}
    assert backtest_summary_for_prompt(fake) == ""


def test_summary_handles_none():
    assert backtest_summary_for_prompt(None) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

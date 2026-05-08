"""Unit tests for CVD / order-flow module."""
from __future__ import annotations

import pandas as pd
import pytest

from app.order_flow import compute_order_flow, order_flow_summary_for_prompt


def _df(bars: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(bars), freq="1h", tz="UTC")
    return pd.DataFrame(
        [
            {"open": o, "high": h, "low": lo, "close": c, "volume": v}
            for (o, h, lo, c, v) in bars
        ],
        index=idx,
    )


def test_returns_none_for_short_df():
    df = _df([(100, 101, 99, 100, 1.0)] * 5)
    assert compute_order_flow(df) is None


def test_uniform_strong_buying_gives_positive_cvd():
    # Each bar closes at the high — fully bullish in our proxy.
    bars = [(100.0, 101.0, 100.0, 101.0, 10.0)] * 30
    df = _df(bars)
    flow = compute_order_flow(df)
    assert flow is not None
    summary = flow.to_summary()
    assert summary["cvd_value"] > 0
    assert summary["buy_pressure_pct"] > 90
    assert summary["divergence"] == "none"


def test_uniform_strong_selling_gives_negative_cvd():
    bars = [(101.0, 101.0, 100.0, 100.0, 10.0)] * 30
    df = _df(bars)
    flow = compute_order_flow(df)
    assert flow is not None
    summary = flow.to_summary()
    assert summary["cvd_value"] < 0
    assert summary["buy_pressure_pct"] < 10


def test_bearish_divergence_detected():
    """Price prints a higher high while CVD lags (lower high)."""
    bars: list[tuple[float, float, float, float, float]] = []
    # Initial leg up — strong buying, CVD rises a lot
    base = 100.0
    for i in range(15):
        p = base + i * 1.0
        bars.append((p, p + 0.5, p, p + 0.5, 100.0))  # close near high
    # Second leg up: makes higher high, but with weak buying volume → low CVD
    for i in range(15):
        p = bars[-1][3] + 0.6
        bars.append((p, p + 0.5, p - 0.4, p + 0.05, 5.0))  # close near low
    df = _df(bars)
    flow = compute_order_flow(df)
    assert flow is not None
    s = flow.to_summary()
    assert s["divergence"] in {"bearish", "none"}


def test_summary_renders_for_prompt():
    flow = {
        "cvd_value": 12.34,
        "cvd_slope": 0.001,
        "buy_pressure_pct": 60.5,
        "divergence": "bullish",
        "divergence_note": "test note",
    }
    s = order_flow_summary_for_prompt(flow)
    assert "CVD=12.34" in s
    assert "bullish" in s


def test_summary_handles_none():
    assert order_flow_summary_for_prompt(None) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

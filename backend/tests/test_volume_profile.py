"""Unit tests for the volume profile module."""
from __future__ import annotations

import pandas as pd
import pytest

from app.volume_profile import compute_volume_profile, volume_profile_summary_for_prompt


def _df(bars: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(bars), freq="1h", tz="UTC")
    return pd.DataFrame(
        [
            {"open": o, "high": h, "low": lo, "close": c, "volume": v}
            for (o, h, lo, c, v) in bars
        ],
        index=idx,
    )


def test_returns_none_for_empty_df():
    df = pd.DataFrame()
    assert compute_volume_profile(df) is None


def test_returns_none_for_short_df():
    df = _df([(100, 101, 99, 100, 1.0)] * 3)
    assert compute_volume_profile(df) is None


def test_basic_profile_keys_and_invariants():
    bars = []
    # Cluster volume around price 100; tiny activity below and above.
    for _ in range(50):
        bars.append((100.0, 100.5, 99.5, 100.0, 100.0))  # heavy at 100
    for _ in range(10):
        bars.append((105.0, 105.5, 104.5, 105.0, 5.0))   # light up
    for _ in range(10):
        bars.append((95.0, 95.5, 94.5, 95.0, 5.0))       # light down

    df = _df(bars)
    vp = compute_volume_profile(df, n_bins=20)
    assert vp is not None
    expected_keys = {
        "poc", "vah", "val", "bin_size", "n_bins", "lookback_bars",
        "position", "distance_to_poc_pct", "hvn", "lvn", "bins",
    }
    assert expected_keys.issubset(vp.keys())
    assert vp["n_bins"] == 20
    assert vp["val"] <= vp["poc"] <= vp["vah"]
    # POC should be near the heavily-traded 100 zone, not 95 or 105.
    assert 99.0 <= vp["poc"] <= 101.0
    # Bin shares sum to ~1
    total_share = sum(b["share"] for b in vp["bins"])
    assert abs(total_share - 1.0) < 1e-6


def test_position_classifies_above_va():
    bars = []
    for _ in range(50):
        bars.append((100.0, 100.5, 99.5, 100.0, 100.0))
    # End the series way above the value area
    bars.append((130.0, 131.0, 129.0, 130.5, 50.0))
    df = _df(bars)
    vp = compute_volume_profile(df, n_bins=24)
    assert vp is not None
    assert vp["position"] == "above_va"
    assert vp["distance_to_poc_pct"] > 0


def test_position_classifies_below_va():
    bars = []
    for _ in range(50):
        bars.append((100.0, 100.5, 99.5, 100.0, 100.0))
    bars.append((70.0, 71.0, 69.0, 70.5, 50.0))
    df = _df(bars)
    vp = compute_volume_profile(df, n_bins=24)
    assert vp is not None
    assert vp["position"] == "below_va"
    assert vp["distance_to_poc_pct"] < 0


def test_summary_renders_for_prompt():
    vp = {
        "poc": 100.0, "vah": 102.0, "val": 98.0, "bin_size": 0.5,
        "n_bins": 24, "lookback_bars": 200, "position": "inside_va",
        "distance_to_poc_pct": 0.5, "hvn": [], "lvn": [], "bins": [],
    }
    s = volume_profile_summary_for_prompt(vp)
    assert "POC 100.0" in s
    assert "VAH 102.0" in s
    assert "VAL 98.0" in s


def test_summary_handles_none():
    assert volume_profile_summary_for_prompt(None) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

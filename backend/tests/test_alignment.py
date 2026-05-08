"""Unit tests for multi-TF alignment scoring."""
from __future__ import annotations

import pytest

from app.alignment import alignment_summary_for_prompt, compute_alignment


def test_full_bullish_alignment():
    current = {"trend": "восходящий", "macd_state": "бычий", "rsi": 60.0, "tf": "1h"}
    htfs = [
        {"tf": "4h", "trend": "восходящий", "macd_state": "бычий", "rsi": 65.0},
        {"tf": "1d", "trend": "восходящий", "macd_state": "бычий", "rsi": 70.0},
    ]
    res = compute_alignment(current, htfs)
    assert res["direction"] == 1
    assert res["score"] >= 90.0
    assert "бычий" in res["label"]
    # Breakdown reflects 3 blocks
    assert len(res["breakdown"]) == 3


def test_full_bearish_alignment():
    current = {"trend": "нисходящий", "macd_state": "медвежий", "rsi": 35.0, "tf": "1h"}
    htfs = [
        {"tf": "4h", "trend": "нисходящий", "macd_state": "медвежий", "rsi": 30.0},
        {"tf": "1d", "trend": "нисходящий", "macd_state": "медвежий", "rsi": 25.0},
    ]
    res = compute_alignment(current, htfs)
    assert res["direction"] == -1
    assert res["score"] >= 90.0


def test_disagreement_flat_direction_and_low_score():
    current = {"trend": "восходящий", "macd_state": "бычий", "rsi": 55.0}
    htfs = [
        {"tf": "4h", "trend": "нисходящий", "macd_state": "медвежий", "rsi": 40.0},
        {"tf": "1d", "trend": "нисходящий", "macd_state": "медвежий", "rsi": 35.0},
    ]
    res = compute_alignment(current, htfs)
    # Higher TFs with bigger weight pull the result negative or to flat.
    assert res["score"] <= 80.0
    assert res["direction"] in (-1, 0)


def test_no_htfs_uses_only_current_block():
    current = {"trend": "восходящий", "macd_state": "бычий", "rsi": 60.0}
    res = compute_alignment(current, [])
    assert len(res["breakdown"]) == 1
    assert res["direction"] == 1


def test_summary_renders_for_prompt():
    res = compute_alignment(
        {"trend": "восходящий", "macd_state": "бычий", "rsi": 60.0},
        [],
    )
    s = alignment_summary_for_prompt(res)
    assert "alignment" in s.lower()


def test_summary_handles_none():
    assert alignment_summary_for_prompt(None) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

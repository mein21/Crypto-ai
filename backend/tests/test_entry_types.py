"""Tests for C2 — entry types (market / limit / stop).

Covers:
- The `_entry_type_consistent` direction-and-distance helper.
- `_validate_signal` rejecting setups whose entry side disagrees with
  entry_type (e.g., long limit above close, long stop below close).
- `_rules_based_fallback` choosing the right entry_type based on
  proximity to support / resistance.
- The Signal schema default and Literal enforcement.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm import (
    _entry_type_consistent,
    _rules_based_fallback,
    _validate_signal,
)
from app.schemas import Signal


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_signal_entry_type_defaults_to_market():
    sig = Signal(direction="flat")
    assert sig.entry_type == "market"


def test_signal_entry_type_accepts_known_values():
    for v in ("market", "limit", "stop"):
        sig = Signal(direction="long", entry_type=v)
        assert sig.entry_type == v


def test_signal_entry_type_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Signal(direction="long", entry_type="hodl")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _entry_type_consistent helper
# ---------------------------------------------------------------------------

def test_entry_type_market_near_close_ok():
    ok, _ = _entry_type_consistent("long", "market", entry=100.2, close=100.0, atr_v=1.0)
    assert ok


def test_entry_type_market_far_from_close_fails():
    ok, reason = _entry_type_consistent("long", "market", entry=110.0, close=100.0, atr_v=1.0)
    assert not ok
    assert "market" in reason


def test_entry_type_long_limit_below_close_ok():
    # Long limit pullback — entry is 1 ATR below close.
    ok, _ = _entry_type_consistent("long", "limit", entry=99.0, close=100.0, atr_v=1.0)
    assert ok


def test_entry_type_long_limit_above_close_fails():
    # Long limit must be BELOW close — entry above is wrong.
    ok, reason = _entry_type_consistent("long", "limit", entry=101.0, close=100.0, atr_v=1.0)
    assert not ok
    assert "ниже close" in reason


def test_entry_type_long_limit_too_far_below_fails():
    # 3 ATR below close is past the 1.5*ATR cap.
    ok, _ = _entry_type_consistent("long", "limit", entry=97.0, close=100.0, atr_v=1.0)
    assert not ok


def test_entry_type_long_stop_above_close_ok():
    # Long stop on breakout — entry above close.
    ok, _ = _entry_type_consistent("long", "stop", entry=101.0, close=100.0, atr_v=1.0)
    assert ok


def test_entry_type_long_stop_below_close_fails():
    # Long stop must be ABOVE close — below is a contradiction.
    ok, reason = _entry_type_consistent("long", "stop", entry=99.0, close=100.0, atr_v=1.0)
    assert not ok
    assert "выше close" in reason


def test_entry_type_short_limit_above_close_ok():
    ok, _ = _entry_type_consistent("short", "limit", entry=101.0, close=100.0, atr_v=1.0)
    assert ok


def test_entry_type_short_limit_below_close_fails():
    ok, reason = _entry_type_consistent("short", "limit", entry=99.0, close=100.0, atr_v=1.0)
    assert not ok
    assert "выше close" in reason


def test_entry_type_short_stop_below_close_ok():
    ok, _ = _entry_type_consistent("short", "stop", entry=99.0, close=100.0, atr_v=1.0)
    assert ok


def test_entry_type_short_stop_above_close_fails():
    ok, reason = _entry_type_consistent("short", "stop", entry=101.0, close=100.0, atr_v=1.0)
    assert not ok
    assert "ниже close" in reason


# ---------------------------------------------------------------------------
# _validate_signal — entry_type sanity gates
# ---------------------------------------------------------------------------

def _summary(close: float = 100.0, atr_v: float = 1.0, atr_pct: float = 1.0, adx_v: float = 25.0) -> dict:
    return {"close": close, "atr": atr_v, "atr_pct": atr_pct, "adx": adx_v}


def test_validate_signal_long_limit_below_close_passes():
    sig = Signal(
        direction="long",
        entry=99.0,
        entry_type="limit",
        stop_loss=98.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        confidence=60,
        rationale="ok",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "long"
    assert out.entry_type == "limit"


def test_validate_signal_long_limit_above_close_flattens():
    # entry above close on a long limit is a contradiction.
    sig = Signal(
        direction="long",
        entry=101.0,
        entry_type="limit",
        stop_loss=99.5,
        take_profit_1=103.0,
        take_profit_2=105.0,
        confidence=60,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"


def test_validate_signal_long_stop_above_close_passes():
    sig = Signal(
        direction="long",
        entry=101.0,
        entry_type="stop",
        stop_loss=100.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
        confidence=60,
        rationale="ok",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "long"
    assert out.entry_type == "stop"


def test_validate_signal_long_stop_below_close_flattens():
    sig = Signal(
        direction="long",
        entry=99.0,
        entry_type="stop",
        stop_loss=98.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        confidence=60,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"


def test_validate_signal_short_limit_above_close_passes():
    sig = Signal(
        direction="short",
        entry=101.0,
        entry_type="limit",
        stop_loss=102.0,
        take_profit_1=98.0,
        take_profit_2=96.0,
        confidence=60,
        rationale="ok",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "short"
    assert out.entry_type == "limit"


def test_validate_signal_short_limit_below_close_flattens():
    sig = Signal(
        direction="short",
        entry=99.0,
        entry_type="limit",
        stop_loss=100.5,
        take_profit_1=97.0,
        take_profit_2=95.0,
        confidence=60,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"


def test_validate_signal_short_stop_below_close_passes():
    sig = Signal(
        direction="short",
        entry=99.0,
        entry_type="stop",
        stop_loss=100.0,
        take_profit_1=97.0,
        take_profit_2=95.0,
        confidence=60,
        rationale="ok",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "short"
    assert out.entry_type == "stop"


def test_validate_signal_short_stop_above_close_flattens():
    sig = Signal(
        direction="short",
        entry=101.0,
        entry_type="stop",
        stop_loss=102.0,
        take_profit_1=98.0,
        take_profit_2=96.0,
        confidence=60,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"


def test_validate_signal_market_default_passes_near_close():
    sig = Signal(
        direction="long",
        entry=100.0,
        # entry_type omitted — should default to "market".
        stop_loss=99.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        confidence=60,
        rationale="ok",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "long"
    assert out.entry_type == "market"


# ---------------------------------------------------------------------------
# _rules_based_fallback — entry_type selection
# ---------------------------------------------------------------------------

def _full_summary(
    *,
    close: float = 100.0,
    trend: str = "восходящий",
    macd_state: str = "бычий",
    rsi_v: float = 50.0,
    atr_v: float = 1.0,
    atr_pct: float = 1.0,
    adx_v: float = 25.0,
    support: list[float] | None = None,
    resistance: list[float] | None = None,
) -> dict:
    return {
        "close": close,
        "ema_fast": close,
        "ema_slow": close * 0.99,
        "ema_long": close * 0.98,
        "rsi": rsi_v,
        "rsi_state": "нейтрально",
        "macd": 0.5,
        "macd_signal": 0.2,
        "macd_hist": 0.3,
        "macd_state": macd_state,
        "bb_lower": close * 0.97,
        "bb_mid": close,
        "bb_upper": close * 1.03,
        "bb_width": 0.06,
        "bb_state": "нормальная",
        "atr": atr_v,
        "atr_pct": atr_pct,
        "adx": adx_v,
        "adx_state": "сильный" if adx_v >= 25 else "слабый",
        "trend": trend,
        "support": support if support is not None else [98.0, 96.0],
        "resistance": resistance if resistance is not None else [104.0, 108.0],
    }


def test_fallback_long_market_when_close_to_support():
    # close = 100, support just 0.3 ATR below (we're sitting on it, not in pullback
    # range). Resistance is comfortably away → no breakout, no deep pullback → market.
    summary = _full_summary(
        close=100.0,
        atr_v=1.0,
        support=[99.7, 95.0],
        resistance=[104.0, 108.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "long"
    assert analysis.signal.entry_type == "market"
    # Market entry sits at close.
    assert analysis.signal.entry == pytest.approx(summary["close"])


def test_fallback_long_limit_when_pullback_within_range():
    # close = 100, support 1 ATR below — we want a pullback to support.
    summary = _full_summary(
        close=100.0,
        atr_v=1.0,
        support=[99.0, 96.0],
        resistance=[104.0, 108.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "long"
    assert analysis.signal.entry_type == "limit"
    # Limit entry sits below close.
    assert analysis.signal.entry < summary["close"]


def test_fallback_long_stop_when_at_resistance():
    # close = 100, resistance just 0.3 ATR above → breakout setup.
    summary = _full_summary(
        close=100.0,
        atr_v=1.0,
        support=[99.5, 97.0],
        resistance=[100.3, 105.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    if analysis.signal.direction == "long":
        # If the RR/HTF gates didn't flatten it, we expect a stop (breakout) entry.
        assert analysis.signal.entry_type == "stop"
        assert analysis.signal.entry > summary["close"]
    else:
        # Acceptable outcome: breakout target too close to provide RR ≥ 1.5 → flat.
        assert analysis.signal.direction == "flat"


def test_fallback_short_limit_when_retest_within_range():
    summary = _full_summary(
        trend="нисходящий",
        macd_state="медвежий",
        close=100.0,
        atr_v=1.0,
        support=[96.0, 92.0],
        resistance=[101.0, 104.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "short"
    assert analysis.signal.entry_type == "limit"
    assert analysis.signal.entry > summary["close"]


def test_fallback_short_stop_when_at_support():
    summary = _full_summary(
        trend="нисходящий",
        macd_state="медвежий",
        close=100.0,
        atr_v=1.0,
        support=[99.7, 95.0],
        resistance=[100.5, 103.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    if analysis.signal.direction == "short":
        assert analysis.signal.entry_type == "stop"
        assert analysis.signal.entry < summary["close"]
    else:
        assert analysis.signal.direction == "flat"


def test_fallback_flat_keeps_entry_type_market():
    # Bad RR → flat → entry_type defaults back to market.
    summary = _full_summary(
        close=100.0,
        atr_v=1.0,
        support=[80.0, 70.0],
        resistance=[100.5, 110.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "flat"
    assert analysis.signal.entry_type == "market"


# ---------------------------------------------------------------------------
# _parse_analysis_json — LLM entry_type round-trip
# ---------------------------------------------------------------------------

def test_parse_analysis_json_accepts_entry_type():
    from app.llm import _parse_analysis_json

    raw = """
    {
      "market_regime": "тренд",
      "trend": "восходящий",
      "key_levels": {"support": [99.0], "resistance": [104.0]},
      "indicators_summary": {"rsi": "50", "macd": "ок", "ema": "ок", "bollinger": "ок"},
      "signal": {
        "direction": "long",
        "entry": 99.5,
        "entry_type": "limit",
        "stop_loss": 98.0,
        "take_profit_1": 103.0,
        "take_profit_2": 105.0,
        "confidence": 60,
        "rationale": "лимит"
      },
      "narrative": "...",
      "risks": ["..."]
    }
    """
    a = _parse_analysis_json(raw, "BTC", "1h")
    assert a.signal.entry_type == "limit"


def test_parse_analysis_json_defaults_entry_type_when_missing():
    from app.llm import _parse_analysis_json

    raw = """
    {
      "market_regime": "тренд",
      "trend": "восходящий",
      "key_levels": {"support": [99.0], "resistance": [104.0]},
      "indicators_summary": {},
      "signal": {
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit_1": 103.0,
        "take_profit_2": 105.0,
        "confidence": 60,
        "rationale": "no entry_type"
      },
      "narrative": "...",
      "risks": []
    }
    """
    a = _parse_analysis_json(raw, "BTC", "1h")
    assert a.signal.entry_type == "market"

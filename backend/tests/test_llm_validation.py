"""Tests for the post-LLM signal validation gate (E1) and the rules-based
fallback fixes (C1/C3/C5/C6/C7).

Goal: pin down the deterministic guardrails added by the strategy review so
we never regress to e.g. accepting an RR<1 trade or a long stop above entry.
"""
from __future__ import annotations

import pytest

from app.llm import (
    MAX_ATR_PCT,
    MIN_RR,
    _compute_rr,
    _decimals_from_precision,
    _htf_disagreement,
    _rules_based_fallback,
    _validate_signal,
)
from app.schemas import Signal


# ---------------------------------------------------------------------------
# Compute RR helper
# ---------------------------------------------------------------------------

def test_compute_rr_long_basic():
    assert _compute_rr("long", entry=100, stop=98, tp=104) == pytest.approx(2.0)


def test_compute_rr_short_basic():
    assert _compute_rr("short", entry=100, stop=102, tp=96) == pytest.approx(2.0)


def test_compute_rr_returns_none_when_inverted():
    # SL above entry on a long is rejected (returns None).
    assert _compute_rr("long", entry=100, stop=102, tp=104) is None
    # TP below entry on a long is rejected.
    assert _compute_rr("long", entry=100, stop=98, tp=99) is None


# ---------------------------------------------------------------------------
# Tick-size precision parsing (G4)
# ---------------------------------------------------------------------------

def test_decimals_from_precision_int():
    assert _decimals_from_precision(2) == 2
    assert _decimals_from_precision(6) == 6


def test_decimals_from_precision_tick_float():
    assert _decimals_from_precision(0.01) == 2
    assert _decimals_from_precision(0.0001) == 4
    assert _decimals_from_precision(1e-8) == 8


def test_decimals_from_precision_invalid():
    assert _decimals_from_precision(None) is None
    assert _decimals_from_precision(0.0) is None  # tick size 0 is invalid
    assert _decimals_from_precision("not-a-number") is None


# ---------------------------------------------------------------------------
# HTF disagreement helper (C5)
# ---------------------------------------------------------------------------

def test_htf_disagreement_all_against_long():
    htf = [{"trend": "нисходящий"}, {"trend": "нисходящий"}, {"trend": "нисходящий"}]
    opposed, agreed = _htf_disagreement("long", htf)
    assert opposed == 3 and agreed == 0


def test_htf_disagreement_mixed():
    htf = [{"trend": "восходящий"}, {"trend": "нисходящий"}, {"trend": "боковой"}]
    opposed, agreed = _htf_disagreement("long", htf)
    assert agreed == 1 and opposed == 1


def test_htf_disagreement_flat_returns_zero():
    htf = [{"trend": "нисходящий"}, {"trend": "нисходящий"}]
    opposed, agreed = _htf_disagreement("flat", htf)
    assert opposed == 0 and agreed == 0


# ---------------------------------------------------------------------------
# _validate_signal — RR gate (C1, E1)
# ---------------------------------------------------------------------------

def _summary(close: float = 100.0, atr_v: float = 1.0, atr_pct: float = 1.0, adx_v: float = 25.0) -> dict:
    return {
        "close": close,
        "atr": atr_v,
        "atr_pct": atr_pct,
        "adx": adx_v,
    }


def test_validate_signal_rr_too_low_flattens():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=100.5,  # RR = 0.5
        take_profit_2=101.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"
    assert out.confidence <= 35
    assert out.entry is None and out.stop_loss is None


def test_validate_signal_passes_when_rr_above_min():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=102.0,  # RR = 2.0
        take_profit_2=104.0,
        confidence=60,
        rationale="ok",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "long"
    assert out.rr is not None and out.rr >= MIN_RR


# ---------------------------------------------------------------------------
# _validate_signal — direction sanity (C3)
# ---------------------------------------------------------------------------

def test_validate_signal_long_with_stop_above_entry_flattens():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=101.0,  # broken — SL above entry on a long
        take_profit_1=104.0,
        take_profit_2=108.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"


def test_validate_signal_short_with_stop_below_entry_flattens():
    sig = Signal(
        direction="short",
        entry=100.0,
        stop_loss=99.0,  # broken — SL below entry on a short
        take_profit_1=95.0,
        take_profit_2=92.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra=None)
    assert out.direction == "flat"


# ---------------------------------------------------------------------------
# _validate_signal — ATR-pct cap (C7)
# ---------------------------------------------------------------------------

def test_validate_signal_high_atr_pct_flattens():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=104.0,
        take_profit_2=108.0,
        confidence=70,
        rationale="seed",
    )
    # ATR ≥ MAX_ATR_PCT triggers the volatility kill-switch.
    out = _validate_signal(sig, _summary(atr_pct=MAX_ATR_PCT + 1.0), extra=None)
    assert out.direction == "flat"


# ---------------------------------------------------------------------------
# _validate_signal — entry must be near close
# ---------------------------------------------------------------------------

def test_validate_signal_entry_far_from_close_flattens():
    sig = Signal(
        direction="long",
        entry=110.0,  # 10% away from close=100 with ATR=1
        stop_loss=99.0,
        take_profit_1=120.0,
        take_profit_2=130.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(close=100.0, atr_v=1.0), extra=None)
    assert out.direction == "flat"


# ---------------------------------------------------------------------------
# _validate_signal — HTF + F&G + ADX confidence penalties
# ---------------------------------------------------------------------------

def test_validate_signal_htf_unanimous_against_penalises_confidence():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        confidence=70,
        rationale="seed",
    )
    htf = [{"trend": "нисходящий"}] * 3
    out = _validate_signal(sig, _summary(), extra={"htf_trends": htf})
    assert out.direction == "long"
    assert out.confidence < 70  # penalty applied


def test_validate_signal_extreme_greed_penalises_long():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra={"fear_greed": {"value": 90, "classification": "Extreme Greed"}})
    assert out.direction == "long"
    assert out.confidence < 70


def test_validate_signal_extreme_fear_penalises_short():
    sig = Signal(
        direction="short",
        entry=100.0,
        stop_loss=101.0,
        take_profit_1=98.0,
        take_profit_2=96.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(), extra={"fear_greed": {"value": 10, "classification": "Extreme Fear"}})
    assert out.direction == "short"
    assert out.confidence < 70


def test_validate_signal_low_adx_penalises_confidence():
    sig = Signal(
        direction="long",
        entry=100.0,
        stop_loss=99.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        confidence=70,
        rationale="seed",
    )
    out = _validate_signal(sig, _summary(adx_v=10.0), extra=None)
    assert out.direction == "long"
    assert out.confidence < 70


# ---------------------------------------------------------------------------
# _rules_based_fallback — end-to-end gates
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


def test_fallback_long_passes_basic_gate():
    summary = _full_summary()
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "long"
    assert analysis.signal.entry is not None
    assert analysis.signal.stop_loss < analysis.signal.entry
    assert analysis.signal.take_profit_1 > analysis.signal.entry
    assert analysis.signal.rr is not None and analysis.signal.rr >= MIN_RR


def test_fallback_extreme_atr_returns_flat():
    summary = _full_summary(atr_pct=MAX_ATR_PCT + 1.0, atr_v=10.0)
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "flat"


def test_fallback_low_rr_returns_flat():
    # Resistance very close to close, support very far → bad RR.
    summary = _full_summary(
        close=100.0,
        atr_v=1.0,
        support=[80.0, 70.0],
        resistance=[100.5, 110.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "flat"


def test_fallback_htf_disagreement_drops_to_flat():
    summary = _full_summary()
    htf = [{"trend": "нисходящий"} for _ in range(3)]
    analysis = _rules_based_fallback("BTC", "1h", summary, extra={"htf_trends": htf})
    assert analysis.signal.direction == "flat"


def test_fallback_short_basic_gate():
    summary = _full_summary(
        trend="нисходящий",
        macd_state="медвежий",
        close=100.0,
        support=[96.0, 92.0],
        resistance=[102.0, 104.0],
    )
    analysis = _rules_based_fallback("BTC", "1h", summary, extra=None)
    assert analysis.signal.direction == "short"
    assert analysis.signal.stop_loss > analysis.signal.entry
    assert analysis.signal.take_profit_1 < analysis.signal.entry
    assert analysis.signal.rr is not None and analysis.signal.rr >= MIN_RR

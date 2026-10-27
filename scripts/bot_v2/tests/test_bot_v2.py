"""Unit tests for pure helpers in bot_v2.py.

These tests stub out env vars + pybit BEFORE importing bot_v2 so we don't need
real Bybit creds to run them.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def bot():
    """Import bot_v2 with stubbed env + pybit."""
    os.environ.update({
        "BYBIT_API_KEY": "test",
        "BYBIT_API_SECRET": "test",
        "TG_TOKEN": "test",
        "TG_CHAT_ID": "0",
        "BOT_TESTNET": "true",
        "BOT_DRY_RUN": "true",
    })

    # Stub pybit so import works without the package installed.
    if "pybit" not in sys.modules:
        pybit = types.ModuleType("pybit")
        unified = types.ModuleType("pybit.unified_trading")

        class _HTTP:  # noqa: D401
            def __init__(self, *a, **kw):
                pass

        unified.HTTP = _HTTP
        pybit.unified_trading = unified
        sys.modules["pybit"] = pybit
        sys.modules["pybit.unified_trading"] = unified

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if "bot_v2" in sys.modules:
        del sys.modules["bot_v2"]
    return importlib.import_module("bot_v2")


# ---------------------------------------------------------------------------
# round_step
# ---------------------------------------------------------------------------
def test_round_step_down(bot):
    assert bot.round_step(0.001234, 0.001, "down") == pytest.approx(0.001, abs=1e-12)
    assert bot.round_step(123.456, 0.5, "down") == pytest.approx(123.0, abs=1e-9)
    assert bot.round_step(99.9999, 1, "down") == pytest.approx(99.0, abs=1e-9)


def test_round_step_up(bot):
    assert bot.round_step(0.001234, 0.001, "up") == pytest.approx(0.002, abs=1e-12)
    assert bot.round_step(123.001, 0.5, "up") == pytest.approx(123.5, abs=1e-9)


def test_round_step_nearest(bot):
    assert bot.round_step(123.7, 0.5, "nearest") == pytest.approx(123.5, abs=1e-9)
    assert bot.round_step(123.8, 0.5, "nearest") == pytest.approx(124.0, abs=1e-9)


def test_round_step_zero_step_passthrough(bot):
    assert bot.round_step(123.456, 0) == 123.456


def test_step_decimals(bot):
    assert bot.step_decimals(0.001) == 3
    assert bot.step_decimals(0.5) == 1
    assert bot.step_decimals(1) == 0
    assert bot.step_decimals(10) == 0
    assert bot.step_decimals(0.0001) == 4


def test_fmt_step(bot):
    # 0.000125 with step 0.001 → 0.000 (rounded down)
    assert bot.fmt_step(0.000125, 0.001) == "0.000"
    # 0.0015 with step 0.001 → 0.001
    assert bot.fmt_step(0.0015, 0.001) == "0.001"
    # 79123.45 with tick 0.1 → "79123.4"
    assert bot.fmt_step(79123.45, 0.1) == "79123.4"


# ---------------------------------------------------------------------------
# calc_ema
# ---------------------------------------------------------------------------
def test_calc_ema_short_series_returns_none(bot):
    assert bot.calc_ema([1.0, 2.0], 5) is None


def test_calc_ema_constant_series(bot):
    # EMA of constant should equal that constant.
    assert bot.calc_ema([5.0] * 30, 10) == pytest.approx(5.0, abs=1e-9)


def test_calc_ema_known_value(bot):
    # Sanity: EMA10 over 1..20 grows monotonically and < 20.
    prices = list(range(1, 21))
    e = bot.calc_ema(prices, 10)
    assert e is not None
    assert 12.0 < e < 20.0


# ---------------------------------------------------------------------------
# calc_atr
# ---------------------------------------------------------------------------
def test_calc_atr_short_series_returns_none(bot):
    assert bot.calc_atr([1.0] * 5, [1.0] * 5, [1.0] * 5, 14) is None


def test_calc_atr_constant_returns_zero(bot):
    n = 20
    atr = bot.calc_atr([10.0] * n, [10.0] * n, [10.0] * n, 14)
    assert atr == pytest.approx(0.0, abs=1e-9)


def test_calc_atr_simple_range(bot):
    # All bars: high=11, low=9, close=10. TR = max(2, 1, 1) = 2 each. ATR = 2.
    n = 30
    h = [11.0] * n
    l = [9.0] * n
    c = [10.0] * n
    atr = bot.calc_atr(h, l, c, 14)
    assert atr == pytest.approx(2.0, abs=1e-9)


# ---------------------------------------------------------------------------
# detect_signal
# ---------------------------------------------------------------------------
def _build_long_setup():
    """24 closes + volumes that should produce a LONG signal."""
    closes = [100.0] * 21  # establish EMA baseline at 100
    closes += [99.0]   # idx -3: red candle (prev_prev->prev fall)
    closes += [102.0]  # idx -2: last closed, rising above EMA
    closes += [101.5]  # idx -1: current (forming, ignored)
    volumes = [100.0] * 22
    volumes.append(500.0)  # idx -2: volume spike
    volumes.append(50.0)   # idx -1: ignored
    return closes, volumes


def test_detect_signal_long(bot):
    closes, volumes = _build_long_setup()
    sig = bot.detect_signal(closes, volumes, ema_period=20, vol_period=20, vol_mult=1.5)
    assert sig == "long"


def test_detect_signal_no_volume(bot):
    closes, volumes = _build_long_setup()
    volumes[-2] = 100.0  # no volume spike
    sig = bot.detect_signal(closes, volumes, ema_period=20, vol_period=20, vol_mult=1.5)
    assert sig is None


def test_detect_signal_short():
    """Mirror of long: short setup should return 'short'."""
    closes = [100.0] * 21
    closes += [101.0]  # idx -3: green (prev_prev->prev rise)
    closes += [98.0]   # idx -2: falling, below EMA
    closes += [98.5]   # idx -1: ignored
    volumes = [100.0] * 22
    volumes.append(500.0)
    volumes.append(50.0)

    # Re-import to get fresh module.
    from bot_v2 import detect_signal
    sig = detect_signal(closes, volumes, ema_period=20, vol_period=20, vol_mult=1.5)
    assert sig == "short"


def test_detect_signal_too_few_bars(bot):
    closes = [100.0, 101.0, 99.0]
    volumes = [100.0, 100.0, 200.0]
    assert bot.detect_signal(closes, volumes, ema_period=20, vol_period=20, vol_mult=1.5) is None


# ---------------------------------------------------------------------------
# compute_qty / compute_levels
# ---------------------------------------------------------------------------
def test_compute_qty_btc_min(bot):
    # margin=$10 × 5x = $50 notional. BTC at $80k → qty=$50/$80k=0.000625.
    # qtyStep=0.001, minQty=0.001. round_down(0.000625 / 0.001) = 0 → 0 < min → 0.
    assert bot.compute_qty(10, 5, 80_000, 0.001, 0.001) == 0.0


def test_compute_qty_xrp(bot):
    # margin=$10 × 10x = $100 notional. XRP at $0.5 → qty=200.
    # qtyStep=1, minQty=1. → 200.
    assert bot.compute_qty(10, 10, 0.5, 1, 1) == pytest.approx(200.0)


def test_compute_qty_below_min(bot):
    # margin=$2 × 1x = $2 notional. BTC $80k → qty 0.000025. min=0.001 → 0.
    assert bot.compute_qty(2, 1, 80_000, 0.001, 0.001) == 0.0


def test_compute_levels_long(bot):
    # entry=100, ATR=2, tick=0.1. SL=100-1*2=98, TP=100+1.5*2=103.
    tp, sl = bot.compute_levels("long", 100.0, 2.0, 0.1)
    assert tp == pytest.approx(103.0, abs=1e-9)
    assert sl == pytest.approx(98.0, abs=1e-9)


def test_compute_levels_short(bot):
    # entry=100, ATR=2, tick=0.1. SL=100+1*2=102, TP=100-1.5*2=97.
    tp, sl = bot.compute_levels("short", 100.0, 2.0, 0.1)
    assert tp == pytest.approx(97.0, abs=1e-9)
    assert sl == pytest.approx(102.0, abs=1e-9)


def test_compute_levels_rounded_to_tick_long(bot):
    # tick=0.5, ATR=1.7. Long: SL away from entry, TP toward entry.
    # SL_raw=100-1*1.7=98.3 → round_down(98.3, 0.5)=98.0 (further below).
    # TP_raw=100+1.5*1.7=102.55 → round_down(102.55, 0.5)=102.5 (closer to entry).
    tp, sl = bot.compute_levels("long", 100.0, 1.7, 0.5)
    assert sl == pytest.approx(98.0, abs=1e-9)
    assert tp == pytest.approx(102.5, abs=1e-9)


def test_compute_levels_rounded_to_tick_short(bot):
    # tick=0.5, ATR=1.7. Short: SL away (above), TP toward entry (above raw).
    # SL_raw=100+1*1.7=101.7 → round_up=102.0 (further above).
    # TP_raw=100-1.5*1.7=97.45 → round_up=97.5 (closer to entry).
    tp, sl = bot.compute_levels("short", 100.0, 1.7, 0.5)
    assert sl == pytest.approx(102.0, abs=1e-9)
    assert tp == pytest.approx(97.5, abs=1e-9)


# ---------------------------------------------------------------------------
# state load/save
# ---------------------------------------------------------------------------
def test_load_state_missing_file(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_FILE", tmp_path / "doesnt-exist.json")
    s = bot.load_state()
    assert s["daily_pnl_usdt"] == 0.0
    assert s["trades_today"] == 0


def test_save_then_load_roundtrip(bot, tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    monkeypatch.setattr(bot, "STATE_FILE", p)
    bot.save_state({"daily_pnl_usdt": -1.5, "trades_today": 3, "last_date": "2026-01-01", "last_pnl_check_ms": 123})
    s = bot.load_state()
    assert s["daily_pnl_usdt"] == -1.5
    assert s["trades_today"] == 3


def test_load_state_corrupt_file_returns_default(bot, tmp_path, monkeypatch):
    p = tmp_path / "broken.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(bot, "STATE_FILE", p)
    monkeypatch.setattr(bot, "send_telegram", lambda *_a, **_k: None)
    s = bot.load_state()
    assert s["daily_pnl_usdt"] == 0.0

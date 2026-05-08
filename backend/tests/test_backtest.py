"""Tests for backend/app/backtest.py.

Synthetic OHLCV data is constructed so each scenario exercises a single
behaviour (TP hit, SL hit, timeout, limit not triggered, etc.) without
relying on any network or LLM calls.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.backtest import (
    DEFAULT_MAX_HOLD,
    DEFAULT_MAX_WAIT,
    Trade,
    _aggregate,
    _max_drawdown,
    _pnl_r,
    _try_trigger,
    _walk_to_exit,
    run_backtest,
    simulate_trade,
)
from app.schemas import Signal


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Tiny helper that turns row-dicts into an OHLCV DataFrame with a UTC index."""
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").astype(float)
    return df


def _bar(ts: str, o: float, h: float, l: float, c: float, v: float = 100.0) -> dict:
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ---------------------------------------------------------------------------
# _pnl_r — R-multiples
# ---------------------------------------------------------------------------


def test_pnl_r_long_full_win():
    # entry 100, stop 95, TP 110 → 1R = 5, win = +2R
    assert _pnl_r("long", 100.0, 95.0, 110.0) == pytest.approx(2.0)


def test_pnl_r_long_full_loss():
    assert _pnl_r("long", 100.0, 95.0, 95.0) == pytest.approx(-1.0)


def test_pnl_r_short_full_win():
    # entry 100, stop 105, TP 90 → 1R = 5, win = +2R
    assert _pnl_r("short", 100.0, 105.0, 90.0) == pytest.approx(2.0)


def test_pnl_r_short_full_loss():
    assert _pnl_r("short", 100.0, 105.0, 105.0) == pytest.approx(-1.0)


def test_pnl_r_zero_risk_is_safe():
    assert _pnl_r("long", 100.0, 100.0, 110.0) == 0.0


# ---------------------------------------------------------------------------
# _try_trigger — entry order filling
# ---------------------------------------------------------------------------


def test_try_trigger_market_returns_next_bar():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 102, 99, 101),
    ])
    assert _try_trigger(df, 0, "long", "market", 100.0, max_wait=5) == 1


def test_try_trigger_market_at_last_bar_returns_none():
    df = _make_df([_bar("2024-01-01", 100, 101, 99, 100)])
    assert _try_trigger(df, 0, "long", "market", 100.0, max_wait=5) is None


def test_try_trigger_limit_long_fills_when_price_dips():
    # signal at idx 0 wants to buy at 95; bar 2 dips to 94 → fill at idx 2
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 102, 97, 101),  # low > 95 → no fill
        _bar("2024-01-03", 101, 102, 94, 100),  # low <= 95 → fill
        _bar("2024-01-04", 100, 102, 99, 101),
    ])
    assert _try_trigger(df, 0, "long", "limit", 95.0, max_wait=5) == 2


def test_try_trigger_limit_long_never_fills_within_window():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 102, 99, 101),
        _bar("2024-01-03", 101, 103, 100, 102),
        _bar("2024-01-04", 102, 104, 101, 103),
    ])
    # Limit price 80 is far below; no bar reaches it.
    assert _try_trigger(df, 0, "long", "limit", 80.0, max_wait=2) is None


def test_try_trigger_stop_long_fills_on_breakout():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 103, 99, 102),  # high < 104 → no fill
        _bar("2024-01-03", 102, 105, 101, 104),  # high >= 104 → fill
    ])
    assert _try_trigger(df, 0, "long", "stop", 104.0, max_wait=5) == 2


def test_try_trigger_limit_short_fills_when_price_rallies():
    # short limit sells at 105; needs high to reach 105
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 104, 99, 102),  # high < 105 → no fill
        _bar("2024-01-03", 102, 106, 101, 104),  # high >= 105 → fill
    ])
    assert _try_trigger(df, 0, "short", "limit", 105.0, max_wait=5) == 2


def test_try_trigger_stop_short_fills_on_breakdown():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 101, 96, 98),  # low > 95 → no fill
        _bar("2024-01-03", 98, 99, 94, 96),    # low <= 95 → fill
    ])
    assert _try_trigger(df, 0, "short", "stop", 95.0, max_wait=5) == 2


# ---------------------------------------------------------------------------
# _walk_to_exit — SL / TP / timeout resolution
# ---------------------------------------------------------------------------


def test_walk_to_exit_long_hits_tp():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),  # entry bar; no exit yet
        _bar("2024-01-02", 100, 110, 99, 108),  # high >= 110 → TP hit
    ])
    out = _walk_to_exit(df, 0, "long", entry=100.0, stop=95.0, take_profit=110.0, max_hold=5)
    assert out["outcome"] == "tp"
    assert out["exit_idx"] == 1
    assert out["exit_price"] == 110.0


def test_walk_to_exit_long_hits_sl():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 102, 94, 95),  # low <= 95 → SL
    ])
    out = _walk_to_exit(df, 0, "long", entry=100.0, stop=95.0, take_profit=110.0, max_hold=5)
    assert out["outcome"] == "stop"
    assert out["exit_idx"] == 1
    assert out["exit_price"] == 95.0


def test_walk_to_exit_long_sl_takes_precedence_when_both_hit():
    # Same bar's range covers both SL and TP — conservative model treats it as a loss.
    df = _make_df([
        _bar("2024-01-01", 100, 110, 94, 100),
    ])
    out = _walk_to_exit(df, 0, "long", entry=100.0, stop=95.0, take_profit=110.0, max_hold=5)
    assert out["outcome"] == "stop"


def test_walk_to_exit_long_timeout():
    rows = [_bar("2024-01-01", 100, 101, 99, 100)]
    for i in range(5):
        rows.append(_bar(f"2024-01-0{i + 2}", 100, 102, 99, 100 + i * 0.1))
    df = _make_df(rows)
    out = _walk_to_exit(df, 0, "long", entry=100.0, stop=80.0, take_profit=120.0, max_hold=3)
    assert out["outcome"] == "timeout"
    # Timeout at start_idx + max_hold → bar 3
    assert out["exit_idx"] == 3
    assert out["exit_price"] == pytest.approx(100.2)


def test_walk_to_exit_short_hits_tp():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 99, 100, 88, 90),  # low <= 90 → TP
    ])
    out = _walk_to_exit(df, 0, "short", entry=100.0, stop=105.0, take_profit=90.0, max_hold=5)
    assert out["outcome"] == "tp"
    assert out["exit_idx"] == 1


def test_walk_to_exit_short_hits_sl():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 106, 99, 105),
    ])
    out = _walk_to_exit(df, 0, "short", entry=100.0, stop=105.0, take_profit=90.0, max_hold=5)
    assert out["outcome"] == "stop"


def test_walk_to_exit_short_sl_takes_precedence():
    df = _make_df([
        _bar("2024-01-01", 100, 106, 89, 100),
    ])
    out = _walk_to_exit(df, 0, "short", entry=100.0, stop=105.0, take_profit=90.0, max_hold=5)
    assert out["outcome"] == "stop"


# ---------------------------------------------------------------------------
# simulate_trade — end to end on a single signal
# ---------------------------------------------------------------------------


def _sig(direction: str, entry_type: str, entry: float, stop: float, tp: float, **kw) -> Signal:
    return Signal(
        direction=direction,
        entry_type=entry_type,
        entry=entry,
        stop_loss=stop,
        take_profit_1=tp,
        take_profit_2=kw.get("take_profit_2"),
        confidence=int(kw.get("confidence", 60)),
        rationale=kw.get("rationale", "test"),
        rr=kw.get("rr"),
    )


def test_simulate_trade_flat_returns_none():
    df = _make_df([_bar("2024-01-01", 100, 101, 99, 100), _bar("2024-01-02", 100, 102, 99, 101)])
    s = Signal(direction="flat", confidence=10, rationale="no setup")
    assert simulate_trade(df, 0, s) is None


def test_simulate_trade_long_market_hits_tp():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 102, 99, 101),
        _bar("2024-01-03", 101, 110, 100, 109),  # next bar after entry — TP at 110 hit
    ])
    sig = _sig("long", "market", entry=100.0, stop=95.0, tp=110.0)
    t = simulate_trade(df, 0, sig)
    assert t is not None
    assert t.triggered is True
    assert t.entry_idx == 1
    assert t.outcome == "tp"
    assert t.pnl_r == pytest.approx(2.0)


def test_simulate_trade_long_limit_not_triggered_within_max_wait():
    # Limit price 90 never reached.
    df = _make_df([_bar(f"2024-01-{i + 1:02d}", 100, 101, 95, 100) for i in range(8)])
    sig = _sig("long", "limit", entry=90.0, stop=85.0, tp=100.0)
    t = simulate_trade(df, 0, sig, max_wait=3, max_hold=5)
    assert t is not None
    assert t.triggered is False
    assert t.outcome == "not_triggered"
    assert t.pnl_r == 0.0


def test_simulate_trade_short_market_hits_sl():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),
        _bar("2024-01-02", 100, 106, 99, 105),
    ])
    sig = _sig("short", "market", entry=100.0, stop=105.0, tp=90.0)
    t = simulate_trade(df, 0, sig)
    assert t is not None
    assert t.triggered is True
    assert t.outcome == "stop"
    assert t.pnl_r == pytest.approx(-1.0)


def test_simulate_trade_long_stop_breakout_then_tp():
    df = _make_df([
        _bar("2024-01-01", 100, 101, 99, 100),  # signal bar
        _bar("2024-01-02", 100, 102, 99, 101),  # waiting; no breakout
        _bar("2024-01-03", 101, 105, 100, 104),  # high >= 104 → entry
        _bar("2024-01-04", 104, 110, 103, 110),  # TP 110 hit
    ])
    sig = _sig("long", "stop", entry=104.0, stop=99.0, tp=110.0)
    t = simulate_trade(df, 0, sig, max_wait=5, max_hold=5)
    assert t is not None
    assert t.triggered is True
    assert t.entry_idx == 2
    assert t.outcome == "tp"
    # 1R = 104-99 = 5; profit 110-104 = 6 → 1.2R
    assert t.pnl_r == pytest.approx(1.2)


def test_simulate_trade_missing_levels_returns_none():
    df = _make_df([_bar("2024-01-01", 100, 101, 99, 100), _bar("2024-01-02", 100, 102, 99, 101)])
    sig = Signal(direction="long", confidence=60, rationale="missing", entry=None)
    assert simulate_trade(df, 0, sig) is None


# ---------------------------------------------------------------------------
# _aggregate / _max_drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_identifies_largest_dip():
    # Curve: 0, +1, +2, +1, -1, +1, +3 → peak 2, trough -1 → dd 3
    assert _max_drawdown([1, 2, 1, -1, 1, 3]) == pytest.approx(3.0)


def test_max_drawdown_monotonic_zero():
    assert _max_drawdown([1, 2, 3, 4]) == 0.0


def _trade(triggered: bool, outcome: str, pnl_r: float, entry_type: str = "market") -> Trade:
    return Trade(
        direction="long",
        entry_type=entry_type,
        signal_idx=0,
        signal_ts=pd.Timestamp("2024-01-01", tz="UTC"),
        planned_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        confidence=60,
        rr_planned=2.0,
        triggered=triggered,
        outcome=outcome,
        pnl_r=pnl_r,
    )


def test_aggregate_basic_metrics():
    trades = [
        _trade(True, "tp", 2.0),
        _trade(True, "stop", -1.0),
        _trade(True, "tp", 1.5),
        _trade(True, "timeout", 0.3),
        _trade(False, "not_triggered", 0.0),  # ignored
    ]
    agg = _aggregate(trades)
    assert agg["n_triggered"] == 4
    assert agg["n_wins"] == 2
    assert agg["n_losses"] == 1
    assert agg["n_timeouts"] == 1
    assert agg["win_rate"] == pytest.approx(0.5)
    assert agg["cumulative_r"] == pytest.approx(2.0 - 1.0 + 1.5 + 0.3)


# ---------------------------------------------------------------------------
# run_backtest — smoke test on synthetic data
# ---------------------------------------------------------------------------


def _synthetic_uptrend_df(n: int = 260) -> pd.DataFrame:
    """An OHLCV series with a clean uptrend so the rules engine emits at least
    one long signal during the lookback window."""
    np.random.seed(7)
    close = 100.0 + np.linspace(0.0, 80.0, n) + np.random.normal(0.0, 0.4, n)
    high = close + np.abs(np.random.normal(0.6, 0.2, n))
    low = close - np.abs(np.random.normal(0.6, 0.2, n))
    open_ = close - np.random.normal(0.0, 0.2, n)
    volume = np.full(n, 1000.0)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_run_backtest_smoke_returns_result_with_expected_shape():
    df = _synthetic_uptrend_df()
    res = run_backtest(df, "BTC", "1h", warmup=200, max_wait=5, max_hold=24)
    assert res.coin == "BTC"
    assert res.timeframe == "1h"
    assert res.n_bars == len(df)
    assert res.n_signals >= 0
    assert res.n_trades >= 0
    assert res.n_trades <= res.n_signals
    # Win rate must be in [0, 1] and the win/loss tally must add up consistently.
    assert 0.0 <= res.win_rate <= 1.0
    assert res.n_wins + res.n_losses + res.n_timeouts == res.n_trades
    # Cumulative R is bounded by n_trades * max_R_per_trade — sanity check it's finite.
    assert math.isfinite(res.cumulative_r)
    assert math.isfinite(res.max_drawdown_r)


def test_run_backtest_raises_on_insufficient_history():
    df = _synthetic_uptrend_df(n=30)
    with pytest.raises(ValueError):
        run_backtest(df, "BTC", "1h", warmup=200)


def test_run_backtest_one_at_a_time_blocks_overlap():
    """When `one_at_a_time=True` the harness must not open a second trade
    while the first is still running. We can verify this by checking that
    every triggered trade ended (had an exit) before the next one started."""
    df = _synthetic_uptrend_df()
    res = run_backtest(df, "BTC", "1h", warmup=200, max_wait=5, max_hold=24)
    triggered = [t for t in res.trades if t.triggered]
    for prev, curr in zip(triggered, triggered[1:]):
        assert prev.exit_idx is not None
        assert curr.signal_idx > prev.exit_idx, (
            f"Overlap detected: trade signalled at idx {curr.signal_idx} while "
            f"prior trade was still open until {prev.exit_idx}."
        )

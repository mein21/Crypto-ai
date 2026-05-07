"""Pre-flight risk checks for every order.

Caller flow (see scheduler.tick):
    1. fetch fresh equity from Bybit
    2. record equity point
    3. call ``check_caps()`` → halts the bot if daily/weekly loss exceeded
    4. call ``size_position()`` → returns the qty to trade

Caps are enforced from the realised P&L stored locally in the trades table.
We don't trust Bybit's position-history endpoint as ground truth because the
worker should be self-contained and resilient to Bybit downtime.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import store
from .config import AutoTradeConfig

DAY_SEC = 86_400
WEEK_SEC = 7 * DAY_SEC


@dataclass
class CapBreach:
    cap: str
    realised_pnl: float
    threshold: float


def check_caps(cfg: AutoTradeConfig, equity_usdt: float, network: str) -> CapBreach | None:
    if equity_usdt <= 0:
        return None
    now = int(time.time())
    daily_pnl = store.realised_pnl(cfg.db_path, now - DAY_SEC, network)
    if daily_pnl < 0 and abs(daily_pnl) >= equity_usdt * (cfg.daily_loss_pct / 100.0):
        return CapBreach(
            cap="daily",
            realised_pnl=daily_pnl,
            threshold=-equity_usdt * cfg.daily_loss_pct / 100.0,
        )
    weekly_pnl = store.realised_pnl(cfg.db_path, now - WEEK_SEC, network)
    if weekly_pnl < 0 and abs(weekly_pnl) >= equity_usdt * (cfg.weekly_loss_pct / 100.0):
        return CapBreach(
            cap="weekly",
            realised_pnl=weekly_pnl,
            threshold=-equity_usdt * cfg.weekly_loss_pct / 100.0,
        )
    return None


def size_position(
    *,
    free_usdt: float,
    entry: float,
    stop_loss: float,
    position_pct: float,
    leverage: float,
    instrument: str,
) -> float:
    """Return base-asset qty so risk per trade ≈ position_pct % of free margin.

    Sizing rule (perp): risk_usdt = free_usdt * position_pct / 100; qty = risk_usdt / |entry-SL|.
    Sizing rule (spot): we treat position_pct as % of free balance to *spend*,
    so qty = free_usdt * position_pct / 100 / entry.
    """
    if entry <= 0:
        return 0.0
    if instrument == "spot":
        notional = free_usdt * (position_pct / 100.0)
        return max(0.0, notional / entry)

    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit <= 0:
        return 0.0
    risk_usdt = free_usdt * (position_pct / 100.0)
    # leverage scales the *notional* we can take on, but our risk per trade is
    # capped by the configured % of free margin regardless of leverage.
    return max(0.0, risk_usdt / risk_per_unit)

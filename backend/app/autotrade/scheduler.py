"""Periodic scan-and-trade loop.

The loop runs on an APScheduler ``AsyncIOScheduler`` started by FastAPI's
``startup`` hook. Each tick:

    1. Skip if we already have ``max_concurrent`` open positions.
    2. Sync open positions with the local trades table — close DB rows whose
       Bybit position no longer exists, recording realised P&L.
    3. Refresh the equity curve sample.
    4. Run risk caps; halt the bot if they were breached.
    5. Move stop-loss to break-even on positions ≥ ``breakeven_at_r``.
    6. Otherwise, scan markets for a fresh trade idea and forward it to Bybit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import risk, scanner, store
from .config import AutoTradeConfig
from .crypto_keys import KeyVault
from .trader import BybitClient, Instrument, Network, now_ts

log = logging.getLogger(__name__)

# Single global instance; we don't run more than one worker per Fly machine.
_scheduler: AsyncIOScheduler | None = None
_run_lock = asyncio.Lock()


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def _coin_to_symbol(coin: str) -> str:
    return f"{coin}USDT"


def _client_for_state(cfg: AutoTradeConfig, vault: KeyVault) -> BybitClient | None:
    state = store.get_state(cfg.db_path)
    if not state:
        return None
    network: Network = state.get("network", "mainnet")  # type: ignore[assignment]
    keys = store.get_keys(cfg.db_path, network)
    if not keys:
        return None
    api_key = vault.decrypt(keys[0])
    api_secret = vault.decrypt(keys[1])
    return BybitClient(api_key, api_secret, network)


async def _tick(cfg: AutoTradeConfig, vault: KeyVault) -> None:
    if _run_lock.locked():
        log.info("autotrade tick already in progress, skipping")
        return
    async with _run_lock:
        await asyncio.to_thread(_tick_sync, cfg, vault)


def _tick_sync(cfg: AutoTradeConfig, vault: KeyVault) -> None:
    state = store.get_state(cfg.db_path)
    if not state.get("running"):
        return
    network: Network = state.get("network", "mainnet")  # type: ignore[assignment]
    instrument: Instrument = state.get("instrument", "linear")  # type: ignore[assignment]
    leverage = float(state.get("leverage") or cfg.default_leverage)
    position_pct = float(state.get("position_pct") or cfg.default_position_pct)

    keys = store.get_keys(cfg.db_path, network)
    if not keys:
        log.warning("autotrade: no API keys for %s, halting", network)
        store.update_state(cfg.db_path, running=0, halted_reason="no_keys")
        return
    client = BybitClient(vault.decrypt(keys[0]), vault.decrypt(keys[1]), network)

    run_id = store.insert_run(cfg.db_path, started_at=now_ts())
    try:
        # --- 1. fresh equity ---------------------------------------------
        try:
            equity, free = client.equity_usdt()
        except Exception as e:  # noqa: BLE001
            log.warning("equity fetch failed: %s", e)
            store.finish_run(cfg.db_path, run_id, action="error", error=str(e))
            return
        store.insert_equity(cfg.db_path, equity, free, len(client.open_positions() if instrument == "linear" else []), network)

        # --- 2. risk caps -------------------------------------------------
        breach = risk.check_caps(cfg, equity, network)
        if breach:
            store.update_state(
                cfg.db_path,
                running=0,
                halted_reason=f"cap_{breach.cap}",
            )
            store.finish_run(
                cfg.db_path, run_id,
                action="halted",
                error=f"{breach.cap} loss cap breached: realised {breach.realised_pnl:.2f} <= {breach.threshold:.2f}",
            )
            log.warning("autotrade halted: %s cap breached", breach.cap)
            return

        # --- 3. reconcile open trades ------------------------------------
        _reconcile_positions(cfg, client, instrument)

        # --- 4. break-even nudge -----------------------------------------
        if instrument == "linear":
            _maybe_move_breakeven(cfg, client)

        # --- 5. capacity check -------------------------------------------
        open_db = store.open_trades(cfg.db_path)
        if len(open_db) >= cfg.max_concurrent:
            store.finish_run(cfg.db_path, run_id, action="skip_full", coins_scanned=0)
            return

        # --- 6. scan ------------------------------------------------------
        result = scanner.scan(
            cfg.coins,
            cfg.timeframes,
            min_confidence=cfg.min_confidence,
            min_rr=cfg.min_rr,
            allow_short=(instrument == "linear"),
        )
        if not result.pick:
            store.finish_run(
                cfg.db_path, run_id,
                action="skip_no_signal",
                coins_scanned=result.coins_scanned,
            )
            return
        pick = result.pick
        sig = pick.analysis.signal

        if sig.entry is None or sig.stop_loss is None or sig.take_profit_1 is None:
            store.finish_run(cfg.db_path, run_id, action="skip_incomplete", coins_scanned=result.coins_scanned)
            return

        # --- 7. place order ----------------------------------------------
        symbol = _coin_to_symbol(pick.coin)
        try:
            info = client.instrument_info(symbol, instrument)
        except Exception as e:  # noqa: BLE001
            store.finish_run(cfg.db_path, run_id, action="error", error=str(e))
            return

        if instrument == "linear":
            client.set_leverage(symbol, leverage)

        qty = risk.size_position(
            free_usdt=free,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            position_pct=position_pct,
            leverage=leverage,
            instrument=instrument,
        )
        if qty <= 0:
            store.finish_run(cfg.db_path, run_id, action="skip_zero_qty", coins_scanned=result.coins_scanned)
            return

        try:
            res = client.place_entry(
                category=instrument,
                symbol=symbol,
                direction=sig.direction,  # type: ignore[arg-type]
                entry_price=sig.entry,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit_1,
                qty=qty,
                info=info,
            )
        except Exception as e:  # noqa: BLE001
            store.finish_run(cfg.db_path, run_id, action="error", error=str(e), coins_scanned=result.coins_scanned)
            return

        order_id = (res.get("orderId") if isinstance(res, dict) else None) or ""
        trade_id = store.insert_trade(
            cfg.db_path,
            run_id=run_id,
            network=network,
            instrument=instrument,
            coin=pick.coin,
            symbol=symbol,
            direction=sig.direction,
            entry_price=sig.entry,
            stop_loss=sig.stop_loss,
            tp1=sig.take_profit_1,
            tp2=sig.take_profit_2,
            qty=qty,
            leverage=leverage if instrument == "linear" else 1.0,
            confidence=int(sig.confidence or 0),
            bybit_order_id=order_id,
        )
        store.finish_run(
            cfg.db_path, run_id,
            action="entered",
            coins_scanned=result.coins_scanned,
            chosen_coin=pick.coin,
            chosen_tf=pick.timeframe,
            chosen_direction=sig.direction,
            chosen_confidence=int(sig.confidence or 0),
            chosen_rr=pick.rr_tp1,
        )
        log.info(
            "AUTOTRADE entered: trade=%d %s %s qty=%.6f conf=%d rr=%.2f order=%s",
            trade_id, pick.coin, sig.direction, qty, sig.confidence or 0, pick.rr_tp1, order_id,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("autotrade tick crashed: %s", e)
        store.finish_run(cfg.db_path, run_id, action="error", error=str(e))


def _reconcile_positions(cfg: AutoTradeConfig, client: BybitClient, instrument: Instrument) -> None:
    """Close DB trades whose Bybit position no longer exists.

    For linear we ask Bybit for live positions. If a previously-open trade
    has size = 0 there now, we mark it closed and write the realised PnL
    based on the entry/closed price diff.
    """
    if instrument != "linear":
        # spot — we treat each entry as resolved when the limit fills + TP hits;
        # without a positions endpoint to read, we skip reconcile here.
        return
    open_db = store.open_trades(cfg.db_path)
    if not open_db:
        return
    live_by_symbol = {p.get("symbol"): p for p in client.open_positions()}
    for trade in open_db:
        sym = trade["symbol"]
        live = live_by_symbol.get(sym)
        if live and float(live.get("size") or 0.0) > 0:
            # still open
            continue
        # closed since last tick — fetch close price via mark price proxy.
        try:
            tickers = client._http.get_tickers(category="linear", symbol=sym)  # noqa: SLF001
            rows = tickers.get("result", {}).get("list", []) if isinstance(tickers, dict) else []
            close_px = float(rows[0].get("lastPrice") or 0.0) if rows else 0.0
        except Exception:  # noqa: BLE001
            close_px = 0.0
        entry = float(trade["entry_price"] or 0.0)
        qty = float(trade["qty"] or 0.0)
        sign = 1 if trade["direction"] == "long" else -1
        pnl = (close_px - entry) * qty * sign if close_px and entry else 0.0
        pnl_pct = (close_px - entry) / entry * 100.0 * sign if close_px and entry else 0.0
        store.update_trade(
            cfg.db_path, trade["id"],
            closed_at=now_ts(),
            close_price=close_px,
            close_reason="reconciled",
            pnl_usdt=pnl,
            pnl_pct=pnl_pct,
        )


def _maybe_move_breakeven(cfg: AutoTradeConfig, client: BybitClient) -> None:
    open_db = store.open_trades(cfg.db_path)
    for trade in open_db:
        if int(trade.get("breakeven_moved") or 0):
            continue
        sym = trade["symbol"]
        live = client.position_for(sym)
        if not live:
            continue
        mark = float(live.get("markPrice") or live.get("avgPrice") or 0.0)
        entry = float(trade["entry_price"] or 0.0)
        sl = float(trade["stop_loss"] or 0.0)
        if entry <= 0 or sl <= 0 or mark <= 0:
            continue
        risk_per = abs(entry - sl)
        if risk_per <= 0:
            continue
        sign = 1 if trade["direction"] == "long" else -1
        r_now = (mark - entry) / risk_per * sign
        if r_now < cfg.breakeven_at_r:
            continue
        try:
            info = client.instrument_info(sym, "linear")
            client.set_breakeven(sym, entry, info)
            store.update_trade(cfg.db_path, trade["id"], breakeven_moved=1)
            log.info("breakeven moved on %s at %.2fR", sym, r_now)
        except Exception as e:  # noqa: BLE001
            log.warning("breakeven move failed for %s: %s", sym, e)


def start(cfg: AutoTradeConfig, vault: KeyVault) -> None:
    global _scheduler
    if _scheduler is not None:
        return
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        _tick, IntervalTrigger(seconds=cfg.scan_interval_sec),
        kwargs={"cfg": cfg, "vault": vault},
        id="autotrade-tick", coalesce=True, max_instances=1, replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info("autotrade scheduler started (every %ds)", cfg.scan_interval_sec)


async def shutdown() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None


async def stop_all_positions(cfg: AutoTradeConfig, vault: KeyVault) -> int:
    """Emergency-close every open Bybit position market and cancel pending orders."""
    state = store.get_state(cfg.db_path)
    network: Network = state.get("network", "mainnet")  # type: ignore[assignment]
    keys = store.get_keys(cfg.db_path, network)
    if not keys:
        return 0
    client = BybitClient(vault.decrypt(keys[0]), vault.decrypt(keys[1]), network)
    closed = 0
    for pos in client.open_positions():
        sym = pos.get("symbol", "")
        side = pos.get("side", "")  # 'Buy' | 'Sell'
        size = float(pos.get("size") or 0.0)
        if not sym or size <= 0:
            continue
        try:
            info = client.instrument_info(sym, "linear")
            client.close_market(
                category="linear",
                symbol=sym,
                direction="long" if side == "Buy" else "short",
                qty=size,
                info=info,
            )
            closed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("emergency close failed for %s: %s", sym, e)
    client.cancel_all("linear")
    client.cancel_all("spot")
    store.update_state(cfg.db_path, running=0, halted_reason="emergency_stop", halted_until=int(time.time()) + 3600)
    return closed

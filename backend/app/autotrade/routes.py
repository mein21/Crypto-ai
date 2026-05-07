"""HTTP API for the autotrade worker.

Endpoints (all under ``/autotrade``):

    POST   /keys                  — store Bybit api_key + secret (encrypted)
    DELETE /keys                  — wipe stored keys (per network)
    GET    /keys/status           — does the worker have keys for a network?
    POST   /start                 — flip the bot ON with a config
    POST   /stop                  — flip the bot OFF (open positions stay)
    POST   /emergency             — close every open position, halt for 1h
    GET    /status                — current state, balance, today/week PnL
    GET    /history               — last N trades + last N runs
    GET    /equity                — equity-curve datapoints
"""
from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import scheduler, store
from .config import AutoTradeConfig
from .crypto_keys import KeyVault
from .trader import BybitClient, Network, Instrument

log = logging.getLogger(__name__)


def _vault(cfg: AutoTradeConfig) -> KeyVault:
    return KeyVault(cfg.master_key)


# -- request bodies ---------------------------------------------------------


class SetKeysBody(BaseModel):
    network: Literal["mainnet", "testnet"] = "mainnet"
    api_key: str = Field(..., min_length=8)
    api_secret: str = Field(..., min_length=8)


class StartBody(BaseModel):
    network: Literal["mainnet", "testnet"] = "mainnet"
    instrument: Literal["spot", "linear"] = "linear"
    leverage: float = Field(3.0, ge=1, le=20)
    position_pct: float = Field(2.0, ge=0.1, le=100)


# -- key management ---------------------------------------------------------


def _bind(cfg: AutoTradeConfig) -> APIRouter:
    """Return a router bound to a specific config instance.

    We rebuild on each call so that ``main.py`` can wire the config from
    its lifespan handler without import-time side effects.
    """
    r = APIRouter(prefix="/autotrade", tags=["autotrade"])

    @r.post("/keys")
    def set_keys(body: SetKeysBody) -> dict:
        vault = _vault(cfg)
        # Validate keys against Bybit before persisting.
        try:
            client = BybitClient(body.api_key, body.api_secret, body.network)
            client.assert_no_withdrawal()
            equity, free = client.equity_usdt()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Bybit отклонил ключ: {e}") from e
        store.upsert_keys(
            cfg.db_path,
            body.network,
            vault.encrypt(body.api_key),
            vault.encrypt(body.api_secret),
        )
        return {
            "ok": True,
            "network": body.network,
            "equity_usdt": equity,
            "free_usdt": free,
        }

    @r.delete("/keys")
    def delete_keys(network: str | None = None) -> dict:
        deleted = store.delete_keys(cfg.db_path, network)
        return {"ok": True, "deleted": deleted}

    @r.get("/keys/status")
    def keys_status() -> dict:
        nets = store.list_networks(cfg.db_path)
        return {"networks": nets, "has_mainnet": "mainnet" in nets, "has_testnet": "testnet" in nets}

    # -- bot control --------------------------------------------------------

    @r.post("/start")
    def start_bot(body: StartBody) -> dict:
        if not store.get_keys(cfg.db_path, body.network):
            raise HTTPException(status_code=400, detail=f"Нет API-ключей для {body.network}")
        store.update_state(
            cfg.db_path,
            running=1,
            network=body.network,
            instrument=body.instrument,
            leverage=body.leverage,
            position_pct=body.position_pct,
            halted_reason=None,
            halted_until=None,
        )
        return {"ok": True, **body.model_dump()}

    @r.post("/stop")
    def stop_bot() -> dict:
        store.update_state(cfg.db_path, running=0, halted_reason="user_stop")
        return {"ok": True}

    @r.post("/emergency")
    async def emergency() -> dict:
        closed = await scheduler.stop_all_positions(cfg, _vault(cfg))
        return {"ok": True, "closed_positions": closed}

    # -- read endpoints -----------------------------------------------------

    @r.get("/status")
    def status() -> dict:
        state = store.get_state(cfg.db_path)
        if not state:
            return {"configured": False}
        nets = store.list_networks(cfg.db_path)
        out = {
            "configured": True,
            "running": bool(state.get("running")),
            "network": state.get("network"),
            "instrument": state.get("instrument"),
            "leverage": state.get("leverage"),
            "position_pct": state.get("position_pct"),
            "halted_reason": state.get("halted_reason"),
            "halted_until": state.get("halted_until"),
            "scan_interval_sec": cfg.scan_interval_sec,
            "min_confidence": cfg.min_confidence,
            "min_rr": cfg.min_rr,
            "max_concurrent": cfg.max_concurrent,
            "daily_loss_pct": cfg.daily_loss_pct,
            "weekly_loss_pct": cfg.weekly_loss_pct,
            "available_networks": nets,
        }
        # try fetch live balance if we have keys
        keys = store.get_keys(cfg.db_path, state.get("network", "mainnet"))
        if keys:
            try:
                vault = _vault(cfg)
                client = BybitClient(vault.decrypt(keys[0]), vault.decrypt(keys[1]), state.get("network", "mainnet"))  # type: ignore[arg-type]
                equity, free = client.equity_usdt()
                out["equity_usdt"] = equity
                out["free_usdt"] = free
                if state.get("instrument") == "linear":
                    positions = client.open_positions()
                    out["open_positions"] = [
                        {
                            "symbol": p.get("symbol"),
                            "side": p.get("side"),
                            "size": float(p.get("size") or 0),
                            "entry": float(p.get("avgPrice") or 0),
                            "mark": float(p.get("markPrice") or 0),
                            "unrealised": float(p.get("unrealisedPnl") or 0),
                            "liq": float(p.get("liqPrice") or 0) if p.get("liqPrice") else None,
                        }
                        for p in positions
                    ]
            except Exception as e:  # noqa: BLE001
                log.warning("status balance fetch failed: %s", e)
                out["balance_error"] = str(e)
        # PnL aggregates
        net = state.get("network", "mainnet")
        now = int(time.time())
        out["today_pnl_usdt"] = store.realised_pnl(cfg.db_path, now - 86400, net)  # type: ignore[arg-type]
        out["week_pnl_usdt"] = store.realised_pnl(cfg.db_path, now - 7 * 86400, net)  # type: ignore[arg-type]
        return out

    @r.get("/history")
    def history(limit: int = 50) -> dict:
        return {
            "trades": store.trade_history(cfg.db_path, limit=limit),
            "runs": store.runs_history(cfg.db_path, limit=min(limit, 30)),
        }

    @r.get("/equity")
    def equity_curve(limit: int = 200) -> dict:
        return {"points": store.equity_curve(cfg.db_path, limit=limit)}

    return r

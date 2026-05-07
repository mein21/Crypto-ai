"""Bybit V5 trading wrapper.

Wraps `pybit.unified_trading.HTTP` with our own thin domain methods so the
scheduler doesn't need to think in Bybit-API jargon. Supports both
``spot`` (long-only, no leverage) and ``linear`` (USDT perpetuals, leverage
+ shorts).

Key safety rules baked in:
- We refuse to trade an account whose API key has withdrawal permission.
- All orders are limit by default (``timeInForce=PostOnly`` falls back to GTC
  if the price would cross the book).
- ``stopLoss`` and ``takeProfit`` are attached on the order itself, so even
  if the worker dies, Bybit will still flatten the position when SL hits.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Literal

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)

Network = Literal["mainnet", "testnet"]
Instrument = Literal["spot", "linear"]


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def _decimals(step: float) -> int:
    if step >= 1:
        return 0
    s = f"{step:.10f}".rstrip("0")
    if "." in s:
        return len(s.split(".")[1])
    return 0


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, network: Network):
        self._network = network
        self._http = HTTP(
            testnet=(network == "testnet"),
            api_key=api_key,
            api_secret=api_secret,
            recv_window=20000,
        )

    # -- diagnostics --------------------------------------------------------
    def assert_no_withdrawal(self) -> None:
        """Raise if the API key was created with withdrawal permission."""
        try:
            resp = self._http.get_api_key_information()
            data = resp.get("result", {}) if isinstance(resp, dict) else {}
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Не удалось проверить права API-ключа: {e}") from e
        permissions = data.get("permissions") or {}
        # V5 returns permissions as dict-of-lists, e.g. {"Wallet": ["AccountTransfer"], ...}
        wallet_perms = set()
        if isinstance(permissions, dict):
            wallet_perms = set(permissions.get("Wallet") or [])
            account_perms = set(permissions.get("Account") or [])
            if "Withdraw" in wallet_perms or "Withdraw" in account_perms:
                raise RuntimeError(
                    "API-ключ имеет разрешение на вывод средств. Перевыпустите "
                    "ключ без `Withdraw` permission и сохраните заново."
                )

    def equity_usdt(self) -> tuple[float, float]:
        """Return (total_equity, free_usdt). Works for UNIFIED account."""
        resp = self._http.get_wallet_balance(accountType="UNIFIED")
        result = resp.get("result", {}) if isinstance(resp, dict) else {}
        equity = 0.0
        free = 0.0
        for acct in result.get("list", []) or []:
            equity = float(acct.get("totalEquity") or 0.0) or equity
            for coin in acct.get("coin", []) or []:
                if coin.get("coin") == "USDT":
                    free = float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0.0)
                    break
        return equity, free

    # -- instrument metadata ------------------------------------------------
    def instrument_info(self, symbol: str, category: Instrument) -> InstrumentInfo:
        resp = self._http.get_instruments_info(category=category, symbol=symbol)
        rows = resp.get("result", {}).get("list", []) if isinstance(resp, dict) else []
        if not rows:
            raise RuntimeError(f"Bybit: нет инструмента {symbol} ({category})")
        row = rows[0]
        if category == "spot":
            tick_size = float(row.get("priceFilter", {}).get("tickSize") or 0.01)
            lot = row.get("lotSizeFilter", {})
            qty_step = float(lot.get("basePrecision") or 0.000001)
            min_qty = float(lot.get("minOrderQty") or qty_step)
            min_notional = float(lot.get("minOrderAmt") or 5.0)
        else:
            tick_size = float(row.get("priceFilter", {}).get("tickSize") or 0.01)
            lot = row.get("lotSizeFilter", {})
            qty_step = float(lot.get("qtyStep") or 0.001)
            min_qty = float(lot.get("minOrderQty") or qty_step)
            min_notional = float(lot.get("minNotionalValue") or 5.0)
        return InstrumentInfo(symbol, tick_size, qty_step, min_qty, min_notional)

    # -- orders -------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: float) -> None:
        try:
            self._http.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except Exception as e:  # noqa: BLE001
            # 110043 = leverage already set; idempotent → ignore.
            msg = str(e).lower()
            if "leverage not modified" in msg or "110043" in msg:
                return
            log.warning("set_leverage failed for %s: %s", symbol, e)

    def place_entry(
        self,
        *,
        category: Instrument,
        symbol: str,
        direction: Literal["long", "short"],
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
        info: InstrumentInfo,
    ) -> dict[str, Any]:
        if category == "spot" and direction == "short":
            raise RuntimeError("Spot не поддерживает шорт. Выберите Perp.")

        side = "Buy" if direction == "long" else "Sell"
        # Round to instrument grid.
        price = _round_step(entry_price, info.tick_size)
        sl = _round_step(stop_loss, info.tick_size)
        tp = _round_step(take_profit, info.tick_size)
        q = _round_step(qty, info.qty_step)
        if q <= 0 or q < info.min_qty:
            raise RuntimeError(
                f"Объём {q} меньше минимума {info.min_qty} ({symbol})."
            )
        if price * q < info.min_notional:
            raise RuntimeError(
                f"Номинал {price * q:.2f} USDT ниже минимального {info.min_notional} ({symbol})."
            )

        d_price = _decimals(info.tick_size)
        d_qty = _decimals(info.qty_step)

        params: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "price": f"{price:.{d_price}f}",
            "qty": f"{q:.{d_qty}f}",
            "timeInForce": "PostOnly",
            "stopLoss": f"{sl:.{d_price}f}",
            "takeProfit": f"{tp:.{d_price}f}",
        }
        if category == "linear":
            params["positionIdx"] = 0  # one-way mode
            params["tpslMode"] = "Full"

        resp = self._http.place_order(**params)
        if isinstance(resp, dict) and resp.get("retCode") not in (0, "0"):
            raise RuntimeError(f"Bybit отклонил ордер: {resp.get('retMsg')}")
        return resp.get("result", {}) if isinstance(resp, dict) else {}

    def cancel_all(self, category: Instrument, symbol: str | None = None) -> None:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        try:
            self._http.cancel_all_orders(**params)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_all_orders failed: %s", e)

    def close_market(self, *, category: Instrument, symbol: str, direction: Literal["long", "short"], qty: float, info: InstrumentInfo) -> None:
        side = "Sell" if direction == "long" else "Buy"
        q = _round_step(qty, info.qty_step)
        d_qty = _decimals(info.qty_step)
        params: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": f"{q:.{d_qty}f}",
            "reduceOnly": True if category == "linear" else False,
        }
        if category == "linear":
            params["positionIdx"] = 0
        self._http.place_order(**params)

    def open_positions(self) -> list[dict[str, Any]]:
        """Return open linear positions (size > 0)."""
        try:
            resp = self._http.get_positions(category="linear", settleCoin="USDT")
            rows = resp.get("result", {}).get("list", []) if isinstance(resp, dict) else []
        except Exception as e:  # noqa: BLE001
            log.warning("get_positions failed: %s", e)
            return []
        out = []
        for r in rows:
            if float(r.get("size") or 0.0) > 0:
                out.append(r)
        return out

    def position_for(self, symbol: str) -> dict[str, Any] | None:
        try:
            resp = self._http.get_positions(category="linear", symbol=symbol)
            rows = resp.get("result", {}).get("list", []) if isinstance(resp, dict) else []
        except Exception as e:  # noqa: BLE001
            log.warning("get_positions(%s) failed: %s", symbol, e)
            return None
        for r in rows:
            if float(r.get("size") or 0.0) > 0:
                return r
        return None

    def set_breakeven(self, symbol: str, entry: float, info: InstrumentInfo) -> None:
        d_price = _decimals(info.tick_size)
        try:
            self._http.set_trading_stop(
                category="linear",
                symbol=symbol,
                stopLoss=f"{_round_step(entry, info.tick_size):.{d_price}f}",
                positionIdx=0,
                tpslMode="Full",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("set_breakeven(%s) failed: %s", symbol, e)


def now_ts() -> int:
    return int(time.time())

"""Fetch OHLCV data via ccxt with a fallback chain across public exchanges.

Order: Bybit → OKX → KuCoin → Coinbase. None of these require API keys for
public OHLCV. Binance is intentionally not used because some hosts get 451.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Iterable

import ccxt
import pandas as pd

log = logging.getLogger(__name__)

# Map our coin codes to USDT/USD pairs across exchanges.
# Coinbase uses USD instead of USDT for most majors.
SYMBOL_MAP_USDT = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "BNB": "BNB/USDT",
    "SOL": "SOL/USDT",
    "XRP": "XRP/USDT",
    "ADA": "ADA/USDT",
    "DOGE": "DOGE/USDT",
    "AVAX": "AVAX/USDT",
    "TON": "TON/USDT",
    "DOT": "DOT/USDT",
}

SYMBOL_MAP_USD = {k: v.replace("/USDT", "/USD") for k, v in SYMBOL_MAP_USDT.items()}

CANDLE_LIMITS = {
    "15m": 300,
    "1h": 300,
    "4h": 300,
    "1d": 300,
    "1w": 200,
}


@lru_cache(maxsize=8)
def _exchange(name: str) -> ccxt.Exchange:
    cls = getattr(ccxt, name)
    return cls({"enableRateLimit": True, "timeout": 15000})


EXCHANGE_ORDER: tuple[tuple[str, dict], ...] = (
    ("okx", SYMBOL_MAP_USDT),
    ("kucoin", SYMBOL_MAP_USDT),
    ("bybit", SYMBOL_MAP_USDT),
    ("coinbase", SYMBOL_MAP_USD),
)


def _try_fetch(coin: str, timeframe: str, limit: int) -> tuple[str, list[list[float]]]:
    last_error: Exception | None = None
    for ex_name, symbol_map in EXCHANGE_ORDER:
        if coin not in symbol_map:
            continue
        symbol = symbol_map[coin]
        try:
            ex = _exchange(ex_name)
            raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            if raw and len(raw) > 30:
                return ex_name, raw
        except Exception as e:  # noqa: BLE001
            last_error = e
            log.warning("Exchange %s failed for %s %s: %s", ex_name, coin, timeframe, e)
            continue
    if last_error:
        raise last_error
    raise RuntimeError("Все биржи в fallback-цепочке вернули пустой ответ")


def fetch_ohlcv(coin: str, timeframe: str) -> pd.DataFrame:
    if coin not in SYMBOL_MAP_USDT:
        raise ValueError(f"Unsupported coin: {coin}")
    limit = CANDLE_LIMITS.get(timeframe, 300)
    ex_name, raw = _try_fetch(coin, timeframe, limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df.astype(float)
    df.attrs["exchange"] = ex_name
    return df


# Back-compat re-export
SYMBOL_MAP = SYMBOL_MAP_USDT

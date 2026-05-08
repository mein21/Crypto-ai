"""Fetch OHLCV data via ccxt with a fallback chain across public exchanges.

Order: OKX → KuCoin → Bybit → Coinbase. None of these require API keys for
public OHLCV. Binance is intentionally not used because some hosts get 451.

The most recent (still-forming) candle is dropped before returning so all
downstream indicators / patterns operate on closed bars only — otherwise
RSI/MACD/engulfing detection oscillate as the live bar updates.
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from threading import Lock
from typing import Any

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

TIMEFRAME_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}

# Cache TTL per timeframe — refresh well before the next bar closes so each
# new closed candle is picked up promptly.
_CACHE_TTL = {
    "15m": 60,
    "1h": 5 * 60,
    "4h": 20 * 60,
    "1d": 60 * 60,
    "1w": 6 * 60 * 60,
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


def _drop_unclosed(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Drop the last bar if it has not finished its timeframe window yet.

    Most exchanges return the currently-forming bar as the last row; using it
    to compute indicators causes signals that flip every few seconds.
    """
    tf_s = TIMEFRAME_SECONDS.get(timeframe)
    if not tf_s or df.empty:
        return df
    now_ts = pd.Timestamp.utcnow()
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    last_open = df.index[-1]
    last_close_due = last_open + pd.Timedelta(seconds=tf_s)
    if now_ts < last_close_due:
        return df.iloc[:-1].copy()
    return df


# --- Tiny TTL cache for OHLCV --------------------------------------------

_OHLCV_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_OHLCV_LOCK = Lock()


def _cache_get(coin: str, timeframe: str) -> pd.DataFrame | None:
    ttl = _CACHE_TTL.get(timeframe, 60)
    key = (coin, timeframe)
    with _OHLCV_LOCK:
        rec = _OHLCV_CACHE.get(key)
    if rec is None:
        return None
    ts, df = rec
    if time.time() - ts > ttl:
        return None
    return df.copy()


def _cache_set(coin: str, timeframe: str, df: pd.DataFrame) -> None:
    key = (coin, timeframe)
    with _OHLCV_LOCK:
        _OHLCV_CACHE[key] = (time.time(), df.copy())


def fetch_ohlcv(coin: str, timeframe: str, *, use_cache: bool = True) -> pd.DataFrame:
    if coin not in SYMBOL_MAP_USDT:
        raise ValueError(f"Unsupported coin: {coin}")
    if use_cache:
        cached = _cache_get(coin, timeframe)
        if cached is not None:
            return cached
    limit = CANDLE_LIMITS.get(timeframe, 300)
    ex_name, raw = _try_fetch(coin, timeframe, limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df.astype(float)
    df.attrs["exchange"] = ex_name
    df = _drop_unclosed(df, timeframe)
    if use_cache:
        _cache_set(coin, timeframe, df)
    return df


@lru_cache(maxsize=64)
def get_market_precision(coin: str) -> dict[str, Any]:
    """Return ccxt market precision for the coin from the first reachable exchange.

    Used by the LLM/rules layer to round entry/SL/TP to the exchange's tick
    grid instead of an arbitrary 6-decimal default.
    """
    for ex_name, symbol_map in EXCHANGE_ORDER:
        if coin not in symbol_map:
            continue
        symbol = symbol_map[coin]
        try:
            ex = _exchange(ex_name)
            ex.load_markets()
            market = ex.markets.get(symbol) or {}
            precision = market.get("precision") or {}
            return {
                "price": precision.get("price"),
                "amount": precision.get("amount"),
                "exchange": ex_name,
            }
        except Exception as e:  # noqa: BLE001
            log.debug("Precision lookup failed on %s for %s: %s", ex_name, coin, e)
            continue
    return {}


# Back-compat re-export
SYMBOL_MAP = SYMBOL_MAP_USDT

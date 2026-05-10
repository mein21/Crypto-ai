"""Auxiliary market context: Fear & Greed index, news, correlation, multi-TF, analytics.

All sources are free and do not require API keys.

- Fear & Greed: https://api.alternative.me/fng/
- News: Cointelegraph RSS per coin tag (free, no auth)
- Correlation: built from ccxt OHLCV (1d, 30 candles)
- Multi-TF trends: built from ccxt OHLCV (the next two higher timeframes)
- Analytics: funding rate + open interest from Binance Futures (public, no key)
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import numpy as np
import pandas as pd

from .data import SYMBOL_MAP_USDT, fetch_ohlcv
from .indicators import compute_all

log = logging.getLogger(__name__)

_HTF_ORDER = ["15m", "1h", "4h", "1d", "1w"]


# ---- Tiny TTL cache ------------------------------------------------------

class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        rec = self._store.get(key)
        if rec is None:
            return None
        ts, value = rec
        if time.time() - ts > ttl:
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)


_cache = _TTLCache()


# ---- Fear & Greed -------------------------------------------------------

def fetch_fear_greed() -> dict | None:
    cached = _cache.get("fng", ttl=1800)  # 30 min
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get("https://api.alternative.me/fng/?limit=1&format=json")
            r.raise_for_status()
            data = r.json()
        rec = (data.get("data") or [{}])[0]
        out = {
            "value": int(rec["value"]),
            "classification": rec.get("value_classification", ""),
            "timestamp": rec.get("timestamp"),
        }
        _cache.set("fng", out)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("Fear&Greed fetch failed: %s", e)
        return None


# ---- News ---------------------------------------------------------------

# Cointelegraph publishes per-tag RSS feeds — no API key required.
_COIN_RSS_TAG = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "bnb-chain",
    "SOL": "solana",
    "XRP": "xrp",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche",
    "TON": "toncoin",
    "DOT": "polkadot",
}

_RSS_BASE = "https://cointelegraph.com/rss/tag/"


def _parse_rss(xml_bytes: bytes, limit: int) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        return []
    out: list[dict] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        ts = 0
        if pub:
            try:
                ts = int(parsedate_to_datetime(pub).timestamp())
            except Exception:  # noqa: BLE001
                ts = 0
        # media:content url — image
        image = ""
        for child in item:
            if child.tag.endswith("}content") and child.attrib.get("medium") == "image":
                image = child.attrib.get("url", "")
                break
        out.append(
            {
                "title": title,
                "url": link,
                "source": "Cointelegraph",
                "ts": ts,
                "image": image,
            }
        )
        if len(out) >= limit:
            break
    return out


def fetch_news_for_coin(coin: str, limit: int = 5) -> list[dict]:
    tag = _COIN_RSS_TAG.get(coin, coin.lower())
    cache_key = f"news:{tag}"
    cached = _cache.get(cache_key, ttl=600)  # 10 min
    if cached is not None:
        return cached[:limit]
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": "crypto-ai/1.0"}) as c:
            r = c.get(f"{_RSS_BASE}{tag}")
            r.raise_for_status()
            items = _parse_rss(r.content, limit=10)
        _cache.set(cache_key, items)
        return items[:limit]
    except Exception as e:  # noqa: BLE001
        log.warning("News fetch failed for %s: %s", coin, e)
        return []


# ---- Multi-timeframe trends --------------------------------------------

def _trend_summary(df: pd.DataFrame) -> dict:
    ind = compute_all(df)
    s = ind.summary()
    first = float(df["close"].iloc[-min(len(df), 30)])
    last = float(df["close"].iloc[-1])
    change_pct = (last / first - 1.0) * 100.0 if first else 0.0
    return {
        "trend": s["trend"],
        "rsi": round(float(s["rsi"]), 1),
        "rsi_state": s["rsi_state"],
        "macd_state": s["macd_state"],
        "change_pct_30bars": round(change_pct, 2),
        "close": float(s["close"]),
    }


def fetch_higher_tf_trends(coin: str, current_tf: str) -> list[dict]:
    """Return trend summaries for the two timeframes higher than current_tf."""
    if current_tf not in _HTF_ORDER:
        return []
    idx = _HTF_ORDER.index(current_tf)
    higher = _HTF_ORDER[idx + 1 : idx + 3]  # up to 2 higher TFs
    out: list[dict] = []
    for tf in higher:
        cache_key = f"htf:{coin}:{tf}"
        cached = _cache.get(cache_key, ttl=300)  # 5 min
        if cached is not None:
            out.append(cached)
            continue
        try:
            df = fetch_ohlcv(coin, tf)
            summary = _trend_summary(df)
            summary["tf"] = tf
            _cache.set(cache_key, summary)
            out.append(summary)
        except Exception as e:  # noqa: BLE001
            log.warning("HTF fetch failed for %s %s: %s", coin, tf, e)
    return out


# ---- Correlation vs BTC -------------------------------------------------

def fetch_correlation_vs_btc(window_days: int = 30) -> dict | None:
    """Return each coin's correlation with BTC over *window_days* daily candles."""
    cached = _cache.get(f"corr_btc:{window_days}", ttl=3600)  # 1 hour
    if cached is not None:
        return cached
    closes: dict[str, pd.Series] = {}
    for coin in SYMBOL_MAP_USDT.keys():
        try:
            df = fetch_ohlcv(coin, "1d")
            s = df["close"].astype(float).tail(window_days)
            if len(s) >= max(10, window_days // 2):
                closes[coin] = s.reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Corr OHLCV failed for %s: %s", coin, e)
    if "BTC" not in closes or len(closes) < 2:
        return None
    min_len = min(len(s) for s in closes.values())
    aligned = pd.DataFrame({k: v.tail(min_len).reset_index(drop=True) for k, v in closes.items()})
    returns = np.log(aligned / aligned.shift(1)).dropna()
    corr = returns.corr().fillna(0.0)
    pairs: list[dict] = []
    for coin in corr.columns:
        if coin == "BTC":
            continue
        pairs.append({"coin": coin, "value": round(float(corr.loc[coin, "BTC"]), 3)})
    pairs.sort(key=lambda p: p["value"], reverse=True)
    out = {
        "pairs": pairs,
        "window_days": window_days,
        "n_observations": int(returns.shape[0]),
    }
    _cache.set(f"corr_btc:{window_days}", out)
    return out


# ---- Analytical centers (funding rate, open interest, long/short ratio) ---

_ANALYTICS_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "AVAX": "AVAXUSDT",
    "TON": "TONUSDT",
    "DOT": "DOTUSDT",
}


def fetch_analytics(coin: str) -> dict | None:
    """Fetch funding rate, open interest, and long/short ratio from Binance Futures."""
    symbol = _ANALYTICS_SYMBOLS.get(coin)
    if not symbol:
        return None
    cache_key = f"analytics:{coin}"
    cached = _cache.get(cache_key, ttl=300)  # 5 min
    if cached is not None:
        return cached
    result: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=10) as c:
            # Funding rate
            r = c.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": symbol, "limit": "1"},
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    result["funding_rate"] = float(data[-1].get("fundingRate", 0))
                    result["funding_rate_pct"] = round(result["funding_rate"] * 100, 4)

            # Open interest
            r2 = c.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": symbol},
            )
            if r2.status_code == 200:
                data2 = r2.json()
                result["open_interest"] = float(data2.get("openInterest", 0))

            # Long/short ratio (top traders)
            r3 = c.get(
                "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
                params={"symbol": symbol, "period": "1h", "limit": "1"},
            )
            if r3.status_code == 200:
                data3 = r3.json()
                if data3:
                    result["long_short_ratio"] = float(data3[-1].get("longShortRatio", 1.0))
                    result["long_account_pct"] = float(data3[-1].get("longAccount", 0.5)) * 100
                    result["short_account_pct"] = float(data3[-1].get("shortAccount", 0.5)) * 100
    except Exception as e:  # noqa: BLE001
        log.warning("Analytics fetch failed for %s: %s", coin, e)

    if result:
        _cache.set(cache_key, result)
        return result
    return None

"""Analytics center: aggregates data from external analytical sources.

Fetches free public data to enrich analysis:
- Long/Short ratio (CoinGlass public API)
- Open Interest changes
- Liquidation data
- Top trader sentiment

All endpoints are free and do not require API keys.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


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

_HEADERS = {
    "User-Agent": "crypto-ai/1.0",
    "Accept": "application/json",
}


def _safe_get(url: str, timeout: int = 10) -> dict | list | None:
    try:
        with httpx.Client(timeout=timeout, headers=_HEADERS) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Analytics fetch failed (%s): %s", url[:80], e)
        return None


def fetch_long_short_ratio(coin: str) -> dict | None:
    """Fetch global long/short ratio from CoinGlass public API."""
    cache_key = f"ls_ratio:{coin}"
    cached = _cache.get(cache_key, ttl=300)  # 5 min
    if cached is not None:
        return cached

    symbol = f"{coin}USDT"
    url = f"https://open-api.coinglass.com/public/v2/long_short?symbol={symbol}&timeType=4"
    data = _safe_get(url)
    if not data or not isinstance(data, dict):
        return None
    records = data.get("data") or []
    if not records:
        return None
    latest = records[0] if isinstance(records, list) else records
    result = {
        "long_rate": float(latest.get("longRate", 50)),
        "short_rate": float(latest.get("shortRate", 50)),
        "long_short_ratio": float(latest.get("longShortRatio", 1.0)),
    }
    _cache.set(cache_key, result)
    return result


def fetch_funding_rate(coin: str) -> dict | None:
    """Fetch current funding rate from Binance."""
    cache_key = f"funding:{coin}"
    cached = _cache.get(cache_key, ttl=300)
    if cached is not None:
        return cached

    symbol = f"{coin}USDT"
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
    data = _safe_get(url)
    if not data or not isinstance(data, list) or len(data) == 0:
        return None
    latest = data[0]
    rate = float(latest.get("fundingRate", 0))
    result = {
        "rate": rate,
        "rate_pct": round(rate * 100, 4),
        "signal": "бычий" if rate < -0.01 else ("медвежий" if rate > 0.05 else "нейтральный"),
    }
    _cache.set(cache_key, result)
    return result


def fetch_open_interest_change(coin: str) -> dict | None:
    """Fetch open interest from Binance Futures."""
    cache_key = f"oi:{coin}"
    cached = _cache.get(cache_key, ttl=300)
    if cached is not None:
        return cached

    symbol = f"{coin}USDT"
    url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={symbol}&period=1h&limit=2"
    data = _safe_get(url)
    if not data or not isinstance(data, list) or len(data) < 2:
        return None
    prev_oi = float(data[0].get("sumOpenInterest", 0))
    curr_oi = float(data[1].get("sumOpenInterest", 0))
    if prev_oi <= 0:
        return None
    change_pct = round((curr_oi / prev_oi - 1.0) * 100, 2)
    result = {
        "current": curr_oi,
        "change_pct": change_pct,
        "signal": "растущий" if change_pct > 2 else ("падающий" if change_pct < -2 else "стабильный"),
    }
    _cache.set(cache_key, result)
    return result


def fetch_top_trader_sentiment(coin: str) -> dict | None:
    """Fetch top trader long/short ratio from Binance."""
    cache_key = f"top_traders:{coin}"
    cached = _cache.get(cache_key, ttl=300)
    if cached is not None:
        return cached

    symbol = f"{coin}USDT"
    url = f"https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol={symbol}&period=1h&limit=1"
    data = _safe_get(url)
    if not data or not isinstance(data, list) or len(data) == 0:
        return None
    latest = data[0]
    long_account = float(latest.get("longAccount", 0.5))
    short_account = float(latest.get("shortAccount", 0.5))
    ratio = float(latest.get("longShortRatio", 1.0))
    result = {
        "long_pct": round(long_account * 100, 1),
        "short_pct": round(short_account * 100, 1),
        "ratio": ratio,
        "bias": "бычий" if ratio > 1.3 else ("медвежий" if ratio < 0.7 else "нейтральный"),
    }
    _cache.set(cache_key, result)
    return result


def fetch_analytics(coin: str) -> dict:
    """Fetch all analytics data for a coin. Returns dict (may have None values)."""
    return {
        "long_short_ratio": fetch_long_short_ratio(coin),
        "funding_rate": fetch_funding_rate(coin),
        "open_interest": fetch_open_interest_change(coin),
        "top_traders": fetch_top_trader_sentiment(coin),
    }


def analytics_summary_for_prompt(analytics: dict) -> str:
    """Format analytics data for LLM prompt."""
    parts: list[str] = []

    ls = analytics.get("long_short_ratio")
    if ls:
        parts.append(
            f"  • Long/Short ratio: {ls['long_short_ratio']:.2f} "
            f"(лонг {ls['long_rate']:.0f}% / шорт {ls['short_rate']:.0f}%)"
        )

    fr = analytics.get("funding_rate")
    if fr:
        parts.append(
            f"  • Funding rate: {fr['rate_pct']}% ({fr['signal']})"
        )

    oi = analytics.get("open_interest")
    if oi:
        parts.append(
            f"  • Open Interest: изменение {oi['change_pct']:+.1f}% за час ({oi['signal']})"
        )

    tt = analytics.get("top_traders")
    if tt:
        parts.append(
            f"  • Топ-трейдеры: {tt['long_pct']}% лонг / {tt['short_pct']}% шорт "
            f"(ratio {tt['ratio']:.2f}, {tt['bias']})"
        )

    return "\n".join(parts) if parts else ""

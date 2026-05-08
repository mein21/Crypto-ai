"""LLM-powered enrichment of crypto news headlines.

For each English headline we ask Groq (Llama 3.3) to return:
  - title_ru:  short Russian rewrite (1 sentence)
  - impact:    "high" / "medium" / "low" — likely market move strength
  - sentiment: "bullish" / "neutral" / "bearish" — direction implied for the coin

All five headlines are batched into one Groq call to keep cold-start time bounded.
Results cached for 30 minutes per (coin, headline-set). On any failure the
original items pass through unchanged so the analyse endpoint never breaks.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: int) -> Any | None:
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
_TTL = 1800  # 30 min

_VALID_IMPACT = {"high", "medium", "low"}
_VALID_SENTIMENT = {"bullish", "neutral", "bearish"}

_SYSTEM_PROMPT = """Ты — финансовый аналитик. Получишь монету и массив англоязычных заголовков новостей про крипту.

Для КАЖДОГО заголовка верни объект:
  - "title_ru":  краткий перевод/пересказ на русском, 1 предложение, без markdown и эмодзи.
  - "impact":    "high" | "medium" | "low" — насколько эта новость может двинуть цену указанной монеты.
  - "sentiment": "bullish" | "neutral" | "bearish" — какой настрой подразумевает заголовок.

Возвращай ТОЛЬКО валидный JSON в формате:
{"items": [{"title_ru": "...", "impact": "...", "sentiment": "..."}, ...]}

Длина массива items должна совпадать с длиной массива headlines, порядок сохраняется."""


def _groq_enrich(coin: str, titles: list[str]) -> list[dict] | None:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    user_payload = json.dumps({"coin": coin, "headlines": titles}, ensure_ascii=False)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
        parsed = json.loads(text)
    except Exception as e:  # noqa: BLE001
        log.warning("News enrich (Groq) failed: %s", e)
        return None

    items = parsed if isinstance(parsed, list) else parsed.get("items")
    if not isinstance(items, list):
        log.warning("News enrich returned unexpected JSON shape: %s", type(parsed).__name__)
        return None
    return items


def _normalise(item: Any) -> dict:
    if not isinstance(item, dict):
        return {}
    out: dict = {}
    title_ru = item.get("title_ru")
    if isinstance(title_ru, str) and title_ru.strip():
        out["title_ru"] = title_ru.strip()
    impact = item.get("impact")
    if isinstance(impact, str) and impact.strip().lower() in _VALID_IMPACT:
        out["impact"] = impact.strip().lower()
    sentiment = item.get("sentiment")
    if isinstance(sentiment, str) and sentiment.strip().lower() in _VALID_SENTIMENT:
        out["sentiment"] = sentiment.strip().lower()
    return out


def enrich_news(coin: str, items: list[dict]) -> list[dict]:
    """Add title_ru / impact / sentiment to each news item.

    Always returns a list of length == len(items). On any failure each item
    keeps its original keys without the extra fields.
    """
    if not items:
        return items
    titles = [str(it.get("title", "")) for it in items]
    cache_key = f"{coin}:{hash(tuple(titles))}"
    cached = _cache.get(cache_key, ttl=_TTL)
    enriched: list[dict] | None
    if cached is not None:
        enriched = cached
    else:
        enriched = _groq_enrich(coin, titles)
        if enriched is not None:
            _cache.set(cache_key, enriched)

    out: list[dict] = []
    for idx, src in enumerate(items):
        merged = dict(src)
        if enriched and idx < len(enriched):
            merged.update(_normalise(enriched[idx]))
        out.append(merged)
    return out

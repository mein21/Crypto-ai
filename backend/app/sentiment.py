"""Sentiment composite (Fear & Greed + headline lexicon + price momentum).

Three deterministic ingredients combined into a 0..100 score with a label:

* **F&G index**: passed through (already 0..100). Weight 0.5.
* **Headline lexicon**: count of bullish vs bearish keywords across the latest
  news titles (English + Russian). Mapped to a 0..100 score where 50 is
  balanced. Weight 0.3.
* **Price momentum**: 1d/24h percent change of the analysed coin. Squashed to
  0..100 around 50. Weight 0.2.

Output dict::

    {
        "score": float,                 # 0..100
        "label": str,                   # "бычий настрой" / "нейтральный" / ...
        "components": {
            "fear_greed": float,
            "news": float,
            "news_breakdown": {"bullish_hits": int, "bearish_hits": int, ...},
            "momentum": float,
            "momentum_pct": float,
        },
    }
"""
from __future__ import annotations

import math

import pandas as pd

# Words that strongly bias headlines bullish/bearish. Lower-cased; substring
# match on the lower-cased title. Curated for crypto-specific copy.
_BULLISH_WORDS = (
    "rally", "surge", "soars", "soar", "bullish", "breakout", "breaks out",
    "all-time high", "ath", "approval", "approved", "etf approval",
    "adoption", "milestone", "buyback", "buy back", "support", "treasury buy",
    "institutional buy", "accumulation", "accumulate", "upgrade",
    "spot etf", "halving", "rebound", "recovery", "momentum",
    "ралли", "рост", "пробо", "одобр", "запуск", "поддержка", "восстанов",
    "халвинг", "рекорд", "максимум",
)

_BEARISH_WORDS = (
    "crash", "plunge", "plunges", "tank", "tanks", "selloff", "sell-off",
    "sell off", "ban", "banned", "lawsuit", "sec sues", "investigation",
    "hack", "hacked", "exploit", "rug pull", "scam", "delist", "delisted",
    "fraud", "warning", "outflow", "outflows", "liquidation", "liquidations",
    "bearish", "down", "drop", "drops", "drops to", "slump",
    "обвал", "падени", "взлом", "хак", "запрет", "расследован", "иск",
    "ликвидаци", "отток", "распродаж", "крах", "медвеж",
)


def _score_news(titles: list[str]) -> tuple[float, dict]:
    """Return (score 0..100, breakdown) for a list of headline strings."""
    if not titles:
        return 50.0, {"bullish_hits": 0, "bearish_hits": 0, "n": 0}
    bull = bear = 0
    for t in titles:
        s = (t or "").lower()
        if not s:
            continue
        bull += sum(1 for w in _BULLISH_WORDS if w in s)
        bear += sum(1 for w in _BEARISH_WORDS if w in s)
    total = bull + bear
    if total == 0:
        return 50.0, {"bullish_hits": 0, "bearish_hits": 0, "n": len(titles)}
    raw = (bull - bear) / total  # in [-1, 1]
    score = round((raw + 1.0) * 50.0, 2)
    return score, {"bullish_hits": int(bull), "bearish_hits": int(bear), "n": len(titles)}


def _score_momentum(pct_change: float) -> float:
    """Map a daily % change to a 0..100 sentiment score around 50."""
    # Logistic-ish squashing: ±10% saturates near 0/100, ±2% gives ~10pt swing.
    score = 50.0 + 40.0 * math.tanh(pct_change / 5.0)
    return round(max(0.0, min(100.0, score)), 2)


def _label_for(score: float) -> str:
    if score >= 70:
        return "бычий настрой"
    if score >= 55:
        return "слабо бычий"
    if score <= 30:
        return "медвежий настрой"
    if score <= 45:
        return "слабо медвежий"
    return "нейтральный"


def compute_sentiment(
    *,
    fear_greed: dict | None,
    news: list[dict] | None,
    daily_close: pd.Series | None,
) -> dict:
    fg_score = float(fear_greed["value"]) if fear_greed and "value" in fear_greed else 50.0

    titles = [n.get("title", "") for n in (news or []) if n.get("title")]
    news_score, news_breakdown = _score_news(titles)

    if daily_close is not None and len(daily_close) >= 2:
        prev = float(daily_close.iloc[-2])
        last = float(daily_close.iloc[-1])
        if prev > 0:
            pct = (last / prev - 1.0) * 100.0
        else:
            pct = 0.0
    else:
        pct = 0.0
    mom_score = _score_momentum(pct)

    composite = round(0.5 * fg_score + 0.3 * news_score + 0.2 * mom_score, 2)
    composite = max(0.0, min(100.0, composite))
    return {
        "score": composite,
        "label": _label_for(composite),
        "components": {
            "fear_greed": round(fg_score, 2),
            "news": news_score,
            "news_breakdown": news_breakdown,
            "momentum": mom_score,
            "momentum_pct": round(pct, 3),
        },
    }


def sentiment_summary_for_prompt(sent: dict | None) -> str:
    if not sent:
        return ""
    c = sent.get("components", {})
    return (
        f"  • Композитный sentiment: {sent.get('score', 0)}/100 "
        f"({sent.get('label', '')}). F&G {c.get('fear_greed', '?')}, "
        f"новости {c.get('news', '?')}, моментум 1d {c.get('momentum_pct', '?')}%"
    )

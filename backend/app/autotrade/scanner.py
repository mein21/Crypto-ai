"""Walk every (coin, timeframe) tuple and pick the highest-confidence trade idea.

Reuses the same primitives the public ``/analyze`` endpoint uses (data fetch,
indicators, LLM). One scheduler tick = at most ``len(coins) * len(timeframes)``
LLM calls; in production this is 10 × 2 = 20 cheap Groq requests.

The returned ``Pick`` is the best entry candidate that:
- has direction ∈ {long, short}
- confidence ≥ ``min_confidence``
- RR(TP1) ≥ ``min_rr``
- (when ``allow_short=False``) only ``long`` directions
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from ..data import fetch_ohlcv
from ..indicators import compute_all
from ..llm import analyze
from ..schemas import Analysis

log = logging.getLogger(__name__)


@dataclass
class Pick:
    coin: str
    timeframe: str
    analysis: Analysis
    rr_tp1: float

    @property
    def direction(self) -> str:
        return self.analysis.signal.direction

    @property
    def confidence(self) -> int:
        return int(self.analysis.signal.confidence or 0)

    @property
    def score(self) -> float:
        return self.confidence * self.rr_tp1


@dataclass
class ScanResult:
    pick: Pick | None
    coins_scanned: int
    errors: list[str]


def _rr_tp1(a: Analysis) -> float:
    s = a.signal
    if s.entry is None or s.stop_loss is None or s.take_profit_1 is None:
        return 0.0
    risk = abs(s.entry - s.stop_loss)
    if risk <= 0:
        return 0.0
    sign = 1 if s.direction == "long" else (-1 if s.direction == "short" else 0)
    if sign == 0:
        return 0.0
    return (s.take_profit_1 - s.entry) / risk * sign


def scan(
    coins: Iterable[str],
    timeframes: Iterable[str],
    *,
    min_confidence: int,
    min_rr: float,
    allow_short: bool,
) -> ScanResult:
    candidates: list[Pick] = []
    errors: list[str] = []
    coins = list(coins)
    timeframes = list(timeframes)
    coins_scanned = 0
    for coin in coins:
        coins_scanned += 1
        for tf in timeframes:
            t0 = time.monotonic()
            try:
                df = fetch_ohlcv(coin, tf)
                if len(df) < 60:
                    continue
                ind = compute_all(df)
                summary = ind.summary()
                analysis = analyze(coin, tf, summary, extra_context={"coin": coin})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{coin}/{tf}: {e}")
                log.warning("scan failed for %s %s: %s", coin, tf, e)
                continue
            d = analysis.signal.direction
            if d not in {"long", "short"}:
                continue
            if d == "short" and not allow_short:
                continue
            conf = int(analysis.signal.confidence or 0)
            if conf < min_confidence:
                continue
            rr = _rr_tp1(analysis)
            if rr < min_rr:
                continue
            candidates.append(Pick(coin, tf, analysis, rr))
            log.info(
                "scan: %s %s direction=%s conf=%d rr=%.2f t=%.1fs",
                coin, tf, d, conf, rr, time.monotonic() - t0,
            )

    pick = max(candidates, key=lambda p: p.score) if candidates else None
    return ScanResult(pick=pick, coins_scanned=coins_scanned, errors=errors)

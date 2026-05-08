"""Classical candlestick pattern detection.

Pure pandas/numpy implementation — no extra runtime dependencies. Each
detector inspects a DataFrame indexed by datetime with columns
['open', 'high', 'low', 'close', 'volume'] and reports hits within a
lookback window relative to the most recent bar.

Patterns implemented (17):
    1-bar  : Doji, Hammer, Inverted Hammer, Hanging Man, Shooting Star,
             Bullish Marubozu, Bearish Marubozu, Spinning Top
    2-bar  : Bullish Engulfing, Bearish Engulfing, Bullish Harami,
             Bearish Harami, Piercing Line, Dark Cloud Cover,
             Tweezer Top, Tweezer Bottom
    3-bar  : Morning Star, Evening Star, Three White Soldiers,
             Three Black Crows, Three Inside Up, Three Inside Down

Each hit is a dict::

    {
        "name": str,            # English canonical name
        "name_ru": str,         # Russian display name
        "bias": str,            # "bullish" | "bearish" | "neutral"
        "kind": str,            # "reversal" | "continuation" | "indecision"
        "strength": int,        # 1..3 (context-adjusted)
        "base_strength": int,   # 1..3 (raw)
        "ts": int,              # unix seconds of the last bar of the pattern
        "bar_index": int,       # negative offset from end of df (-1 = last)
        "context": str,         # human-readable context summary
    }
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shapes & helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Bar:
    o: float
    h: float
    l: float
    c: float
    body: float
    body_top: float
    body_bot: float
    upper: float
    lower: float
    rng: float
    bullish: bool
    bearish: bool

    @classmethod
    def from_row(cls, row: pd.Series) -> "_Bar":
        o = float(row["open"])
        h = float(row["high"])
        lo = float(row["low"])
        c = float(row["close"])
        body_top = max(o, c)
        body_bot = min(o, c)
        body = body_top - body_bot
        return cls(
            o=o,
            h=h,
            l=lo,
            c=c,
            body=body,
            body_top=body_top,
            body_bot=body_bot,
            upper=h - body_top,
            lower=body_bot - lo,
            rng=max(h - lo, 1e-12),
            bullish=c > o,
            bearish=c < o,
        )


def _avg_body(df: pd.DataFrame, window: int = 14) -> float:
    body = (df["close"] - df["open"]).abs()
    tail = body.tail(window)
    if tail.empty:
        return 0.0
    val = float(tail.mean())
    return val if val > 0 else 0.0


def _local_trend(df: pd.DataFrame, end_idx_exclusive: int, lookback: int = 8) -> str:
    """Trend over the bars BEFORE `end_idx_exclusive`. Returns 'up'|'down'|'flat'."""
    start = max(0, end_idx_exclusive - lookback)
    closes = df["close"].iloc[start:end_idx_exclusive]
    if len(closes) < 3:
        return "flat"
    first = float(closes.iloc[0])
    last = float(closes.iloc[-1])
    if first <= 0:
        return "flat"
    pct = (last / first - 1.0) * 100.0
    if pct >= 1.0:
        return "up"
    if pct <= -1.0:
        return "down"
    return "flat"


def _ts(df: pd.DataFrame, idx: int) -> int:
    """Unix seconds for bar at row position `idx`."""
    try:
        ts = df.index[idx]
        if hasattr(ts, "timestamp"):
            return int(ts.timestamp())
        return int(pd.Timestamp(ts).timestamp())
    except Exception:  # noqa: BLE001
        return 0


def _near_level(price: float, levels: Optional[list[float]], atr: float) -> Optional[float]:
    if not levels:
        return None
    tol = max(atr * 0.5, price * 0.003)
    nearest = min(levels, key=lambda x: abs(x - price))
    if abs(nearest - price) <= tol:
        return float(nearest)
    return None


# ---------------------------------------------------------------------------
# Single-bar shape predicates
# ---------------------------------------------------------------------------

def _is_doji(b: _Bar) -> bool:
    return b.rng > 0 and b.body <= 0.1 * b.rng


def _is_hammer_shape(b: _Bar) -> bool:
    """Long lower wick, tiny upper wick, body in upper part of range."""
    if b.body <= 0:
        return False
    return (
        b.lower >= 2.0 * b.body
        and b.upper <= 0.3 * b.body
        and (b.h - b.body_top) <= 0.25 * b.rng
    )


def _is_inverted_hammer_shape(b: _Bar) -> bool:
    if b.body <= 0:
        return False
    return (
        b.upper >= 2.0 * b.body
        and b.lower <= 0.3 * b.body
        and (b.body_bot - b.l) <= 0.25 * b.rng
    )


def _is_marubozu_bullish(b: _Bar, ab: float) -> bool:
    return b.bullish and b.body >= 0.9 * b.rng and (ab == 0 or b.body >= 1.1 * ab)


def _is_marubozu_bearish(b: _Bar, ab: float) -> bool:
    return b.bearish and b.body >= 0.9 * b.rng and (ab == 0 or b.body >= 1.1 * ab)


def _is_spinning_top(b: _Bar, ab: float) -> bool:
    if b.body <= 0 or _is_doji(b):
        return False
    return (
        ab > 0
        and b.body <= 0.5 * ab
        and b.upper >= b.body
        and b.lower >= b.body
    )


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------
# Each returns dict with name/bias/kind/base_strength when matched, else None.

def _detect_one_bar(df: pd.DataFrame, i: int, ab: float) -> list[dict]:
    """Return all 1-bar pattern hits at position `i`."""
    if i < 0 or i >= len(df):
        return []
    b = _Bar.from_row(df.iloc[i])
    trend_before = _local_trend(df, i, lookback=8)
    hits: list[dict] = []

    # Doji
    if _is_doji(b):
        hits.append(
            {
                "name": "Doji",
                "name_ru": "Дожи",
                "bias": "neutral",
                "kind": "indecision",
                "base_strength": 1,
            }
        )

    # Marubozu (continuation, strong-body candle without shadows)
    if _is_marubozu_bullish(b, ab):
        hits.append(
            {
                "name": "Bullish Marubozu",
                "name_ru": "Бычий марубозу",
                "bias": "bullish",
                "kind": "continuation",
                "base_strength": 2,
            }
        )
    elif _is_marubozu_bearish(b, ab):
        hits.append(
            {
                "name": "Bearish Marubozu",
                "name_ru": "Медвежий марубозу",
                "bias": "bearish",
                "kind": "continuation",
                "base_strength": 2,
            }
        )

    # Hammer / Hanging Man (same shape, different trend context)
    if _is_hammer_shape(b):
        if trend_before == "down":
            hits.append(
                {
                    "name": "Hammer",
                    "name_ru": "Молот",
                    "bias": "bullish",
                    "kind": "reversal",
                    "base_strength": 2,
                }
            )
        elif trend_before == "up":
            hits.append(
                {
                    "name": "Hanging Man",
                    "name_ru": "Повешенный",
                    "bias": "bearish",
                    "kind": "reversal",
                    "base_strength": 2,
                }
            )

    # Inverted Hammer / Shooting Star
    if _is_inverted_hammer_shape(b):
        if trend_before == "down":
            hits.append(
                {
                    "name": "Inverted Hammer",
                    "name_ru": "Перевёрнутый молот",
                    "bias": "bullish",
                    "kind": "reversal",
                    "base_strength": 2,
                }
            )
        elif trend_before == "up":
            hits.append(
                {
                    "name": "Shooting Star",
                    "name_ru": "Падающая звезда",
                    "bias": "bearish",
                    "kind": "reversal",
                    "base_strength": 2,
                }
            )

    # Spinning Top (only useful in a trend → indecision)
    if _is_spinning_top(b, ab) and trend_before != "flat":
        hits.append(
            {
                "name": "Spinning Top",
                "name_ru": "Волчок",
                "bias": "neutral",
                "kind": "indecision",
                "base_strength": 1,
            }
        )

    return hits


def _detect_two_bar(df: pd.DataFrame, i: int, ab: float) -> list[dict]:
    """Return all 2-bar pattern hits ending at position `i`."""
    if i < 1 or i >= len(df):
        return []
    p = _Bar.from_row(df.iloc[i - 1])
    c = _Bar.from_row(df.iloc[i])
    trend_before = _local_trend(df, i - 1, lookback=8)
    hits: list[dict] = []

    long_p = ab > 0 and p.body >= 0.8 * ab
    long_c = ab > 0 and c.body >= 0.8 * ab

    # Bullish Engulfing (after downtrend)
    if (
        p.bearish
        and c.bullish
        and c.body > p.body
        and c.o <= p.c
        and c.c >= p.o
        and trend_before == "down"
    ):
        hits.append(
            {
                "name": "Bullish Engulfing",
                "name_ru": "Бычье поглощение",
                "bias": "bullish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Bearish Engulfing (after uptrend)
    if (
        p.bullish
        and c.bearish
        and c.body > p.body
        and c.o >= p.c
        and c.c <= p.o
        and trend_before == "up"
    ):
        hits.append(
            {
                "name": "Bearish Engulfing",
                "name_ru": "Медвежье поглощение",
                "bias": "bearish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Bullish Harami: prev bearish long body, curr small bullish inside prev body
    if (
        p.bearish
        and long_p
        and c.bullish
        and c.body_top <= p.body_top
        and c.body_bot >= p.body_bot
        and c.body < p.body * 0.6
        and trend_before == "down"
    ):
        hits.append(
            {
                "name": "Bullish Harami",
                "name_ru": "Бычий харами",
                "bias": "bullish",
                "kind": "reversal",
                "base_strength": 2,
            }
        )

    # Bearish Harami
    if (
        p.bullish
        and long_p
        and c.bearish
        and c.body_top <= p.body_top
        and c.body_bot >= p.body_bot
        and c.body < p.body * 0.6
        and trend_before == "up"
    ):
        hits.append(
            {
                "name": "Bearish Harami",
                "name_ru": "Медвежий харами",
                "bias": "bearish",
                "kind": "reversal",
                "base_strength": 2,
            }
        )

    # Piercing Line: prev bearish long, curr bullish opens below prev low,
    # closes above midpoint of prev body but below prev open
    p_mid = (p.o + p.c) / 2.0
    if (
        p.bearish
        and long_p
        and c.bullish
        and long_c
        and c.o < p.l
        and c.c > p_mid
        and c.c < p.o
        and trend_before == "down"
    ):
        hits.append(
            {
                "name": "Piercing Line",
                "name_ru": "Просвет в облаках",
                "bias": "bullish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Dark Cloud Cover: opposite
    if (
        p.bullish
        and long_p
        and c.bearish
        and long_c
        and c.o > p.h
        and c.c < p_mid
        and c.c > p.o
        and trend_before == "up"
    ):
        hits.append(
            {
                "name": "Dark Cloud Cover",
                "name_ru": "Завеса из тёмных облаков",
                "bias": "bearish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Tweezer Top: similar highs after uptrend
    high_tol = max(0.001 * c.h, p.rng * 0.05)
    if (
        trend_before == "up"
        and abs(p.h - c.h) <= high_tol
        and (p.bullish or _is_inverted_hammer_shape(p) or _is_doji(p))
        and (c.bearish or _is_inverted_hammer_shape(c) or _is_doji(c))
    ):
        hits.append(
            {
                "name": "Tweezer Top",
                "name_ru": "Пинцет сверху",
                "bias": "bearish",
                "kind": "reversal",
                "base_strength": 2,
            }
        )

    # Tweezer Bottom: similar lows after downtrend
    low_tol = max(0.001 * c.l, p.rng * 0.05)
    if (
        trend_before == "down"
        and abs(p.l - c.l) <= low_tol
        and (p.bearish or _is_hammer_shape(p) or _is_doji(p))
        and (c.bullish or _is_hammer_shape(c) or _is_doji(c))
    ):
        hits.append(
            {
                "name": "Tweezer Bottom",
                "name_ru": "Пинцет снизу",
                "bias": "bullish",
                "kind": "reversal",
                "base_strength": 2,
            }
        )

    return hits


def _detect_three_bar(df: pd.DataFrame, i: int, ab: float) -> list[dict]:
    """Return all 3-bar pattern hits ending at position `i`."""
    if i < 2 or i >= len(df):
        return []
    a = _Bar.from_row(df.iloc[i - 2])
    b = _Bar.from_row(df.iloc[i - 1])
    c = _Bar.from_row(df.iloc[i])
    trend_before = _local_trend(df, i - 2, lookback=8)
    hits: list[dict] = []

    long_a = ab > 0 and a.body >= 0.8 * ab
    long_c = ab > 0 and c.body >= 0.8 * ab
    small_b = ab > 0 and b.body <= 0.4 * ab
    a_mid = (a.o + a.c) / 2.0

    # Morning Star (bullish reversal)
    if (
        a.bearish
        and long_a
        and small_b
        and b.body_top < a.body_bot  # gap down on body
        and c.bullish
        and c.c > a_mid
        and trend_before == "down"
    ):
        hits.append(
            {
                "name": "Morning Star",
                "name_ru": "Утренняя звезда",
                "bias": "bullish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Evening Star (bearish reversal)
    if (
        a.bullish
        and long_a
        and small_b
        and b.body_bot > a.body_top
        and c.bearish
        and c.c < a_mid
        and trend_before == "up"
    ):
        hits.append(
            {
                "name": "Evening Star",
                "name_ru": "Вечерняя звезда",
                "bias": "bearish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Three White Soldiers
    if (
        a.bullish
        and b.bullish
        and c.bullish
        and b.c > a.c
        and c.c > b.c
        and a.o < b.o < c.o
        and b.o <= a.c
        and c.o <= b.c
        and a.upper <= 0.3 * a.body
        and b.upper <= 0.3 * b.body
        and c.upper <= 0.3 * c.body
    ):
        hits.append(
            {
                "name": "Three White Soldiers",
                "name_ru": "Три белых солдата",
                "bias": "bullish",
                "kind": "continuation",
                "base_strength": 3,
            }
        )

    # Three Black Crows
    if (
        a.bearish
        and b.bearish
        and c.bearish
        and b.c < a.c
        and c.c < b.c
        and a.o > b.o > c.o
        and b.o >= a.c
        and c.o >= b.c
        and a.lower <= 0.3 * a.body
        and b.lower <= 0.3 * b.body
        and c.lower <= 0.3 * c.body
    ):
        hits.append(
            {
                "name": "Three Black Crows",
                "name_ru": "Три чёрные вороны",
                "bias": "bearish",
                "kind": "continuation",
                "base_strength": 3,
            }
        )

    # Three Inside Up: bearish long → bullish harami inside → bullish
    # close above first open
    if (
        a.bearish
        and long_a
        and b.bullish
        and b.body_top <= a.body_top
        and b.body_bot >= a.body_bot
        and b.body < a.body * 0.7
        and c.bullish
        and c.c > a.o
        and trend_before == "down"
    ):
        hits.append(
            {
                "name": "Three Inside Up",
                "name_ru": "Три внутри вверх",
                "bias": "bullish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    # Three Inside Down
    if (
        a.bullish
        and long_a
        and b.bearish
        and b.body_top <= a.body_top
        and b.body_bot >= a.body_bot
        and b.body < a.body * 0.7
        and c.bearish
        and c.c < a.o
        and trend_before == "up"
    ):
        hits.append(
            {
                "name": "Three Inside Down",
                "name_ru": "Три внутри вниз",
                "bias": "bearish",
                "kind": "reversal",
                "base_strength": 3,
            }
        )

    return hits


# ---------------------------------------------------------------------------
# Context scoring
# ---------------------------------------------------------------------------

def _score_and_describe(
    hit: dict,
    df: pd.DataFrame,
    i: int,
    support: Optional[list[float]],
    resistance: Optional[list[float]],
    atr: float,
    trend_label: Optional[str],
) -> dict:
    """Adjust strength by S/R proximity & trend alignment, build context string."""
    bias = hit["bias"]
    kind = hit["kind"]
    base = int(hit["base_strength"])
    score = base
    notes: list[str] = []

    bar = _Bar.from_row(df.iloc[i])

    # S/R proximity boost
    near_sup = _near_level(bar.l, support, atr) if bias == "bullish" else None
    near_res = _near_level(bar.h, resistance, atr) if bias == "bearish" else None
    if near_sup is not None:
        score += 1
        notes.append(f"у поддержки {_fmt_price(near_sup)}")
    if near_res is not None:
        score += 1
        notes.append(f"у сопротивления {_fmt_price(near_res)}")

    # Trend alignment
    if trend_label:
        if kind == "reversal":
            if (bias == "bullish" and "нисход" in trend_label) or (
                bias == "bearish" and "восход" in trend_label
            ):
                score += 1
                notes.append("против тренда → разворотный")
            elif (bias == "bullish" and "восход" in trend_label) or (
                bias == "bearish" and "нисход" in trend_label
            ):
                score -= 1
                notes.append("по направлению тренда — слабый разворот")
        elif kind == "continuation":
            if (bias == "bullish" and "восход" in trend_label) or (
                bias == "bearish" and "нисход" in trend_label
            ):
                score += 1
                notes.append("подтверждает тренд")
            elif (bias == "bullish" and "нисход" in trend_label) or (
                bias == "bearish" and "восход" in trend_label
            ):
                score -= 1
                notes.append("против тренда — сомнительный")

    score = max(1, min(3, score))
    bar_offset = i - len(df)  # negative offset, -1 = last bar
    return {
        "name": hit["name"],
        "name_ru": hit["name_ru"],
        "bias": bias,
        "kind": kind,
        "strength": score,
        "base_strength": base,
        "ts": _ts(df, i),
        "bar_index": bar_offset,
        "context": "; ".join(notes) if notes else "вне явных уровней и тренда",
    }


def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}".replace(",", " ")
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.4f}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_patterns(
    df: pd.DataFrame,
    *,
    lookback: int = 10,
    support: Optional[list[float]] = None,
    resistance: Optional[list[float]] = None,
    atr: float = 0.0,
    trend_label: Optional[str] = None,
) -> list[dict]:
    """Detect candlestick patterns in the last `lookback` bars of `df`.

    The window slides over bar positions [N - lookback ... N - 1] (inclusive
    of the most recently closed bar). For each position we run all 1-bar,
    2-bar, and 3-bar detectors that fit; hits are scored by S/R proximity
    and trend alignment, then sorted by recency (newest first).
    """
    n = len(df)
    if n < 5:
        return []
    ab = _avg_body(df, window=14)
    start = max(2, n - lookback)
    raw_hits: list[dict] = []
    for i in range(start, n):
        for hit in _detect_one_bar(df, i, ab):
            raw_hits.append((i, hit))
        for hit in _detect_two_bar(df, i, ab):
            raw_hits.append((i, hit))
        for hit in _detect_three_bar(df, i, ab):
            raw_hits.append((i, hit))

    scored: list[dict] = [
        _score_and_describe(hit, df, i, support, resistance, atr, trend_label)
        for (i, hit) in raw_hits
    ]
    # Sort: newest first, then strongest
    scored.sort(key=lambda x: (-(x["bar_index"]), -x["strength"]))
    return scored


def patterns_summary_for_prompt(patterns: list[dict], limit: int = 6) -> str:
    """Build a short bullet list of the top patterns for the LLM prompt."""
    if not patterns:
        return ""
    lines: list[str] = []
    for p in patterns[:limit]:
        bias_ru = {"bullish": "бычий", "bearish": "медвежий", "neutral": "нейтр."}.get(
            p["bias"], p["bias"]
        )
        lines.append(
            f"  • {p['name_ru']} ({bias_ru}, сила {p['strength']}/3, {p['context']})"
        )
    return "\n".join(lines)

"""Multi-timeframe alignment score.

Combines the trend / RSI / MACD signals across the current timeframe and the
two higher timeframes that already come from `context.fetch_higher_tf_trends`.
The score is normalised to a 0..100 scale plus a direction in {-1, 0, 1}.

Mapping per-TF (each contribution is in [-1.5, +1.5]):
    trend       : "восходящий"=+1, "боковой"=0, "нисходящий"=-1
    macd        : "бычий"=+1, "нейтрально"=0, "медвежий"=-1
    rsi         : >55 → +0.5, <45 → -0.5, else 0

The current TF gets weight 1.0; higher TFs get weight 1.5 and 2.0 (the further
out, the more weight). The raw score is divided by the max possible to get
0..100 in absolute value, with a sign for direction.
"""
from __future__ import annotations


def _score_trend(label: str | None) -> float:
    if not label:
        return 0.0
    if "восход" in label:
        return 1.0
    if "нисход" in label:
        return -1.0
    return 0.0


def _score_macd(label: str | None) -> float:
    if not label:
        return 0.0
    if "быч" in label:
        return 1.0
    if "медвеж" in label:
        return -1.0
    return 0.0


def _score_rsi(value: float | None) -> float:
    if value is None:
        return 0.0
    if value > 55:
        return 0.5
    if value < 45:
        return -0.5
    return 0.0


def _score_block(trend: str | None, macd: str | None, rsi: float | None) -> float:
    return _score_trend(trend) + _score_macd(macd) + _score_rsi(rsi)


def _label_for(score: float, direction: int) -> str:
    abs_s = abs(score)
    if direction == 0 or abs_s < 25:
        return "рассинхронизация"
    side = "бычий" if direction > 0 else "медвежий"
    if abs_s >= 75:
        return f"синхронно {side}"
    if abs_s >= 50:
        return f"в основном {side}"
    return f"слабо {side}"


def compute_alignment(current: dict, htf_trends: list[dict]) -> dict:
    """Build a multi-TF alignment summary.

    `current` must carry `trend`, `macd_state`, `rsi`. `htf_trends` is the
    list returned by `fetch_higher_tf_trends` (each item with `tf`, `trend`,
    `macd_state`, `rsi`).
    """
    weights = [(current, 1.0)]
    for i, h in enumerate(htf_trends[:2]):
        weights.append((h, 1.5 + i * 0.5))  # 1.5 then 2.0

    total = 0.0
    max_total = 0.0
    breakdown: list[dict] = []
    for block, w in weights:
        s = _score_block(
            trend=block.get("trend"),
            macd=block.get("macd_state"),
            rsi=block.get("rsi"),
        )
        total += s * w
        max_total += 2.5 * w  # max per block: 1+1+0.5
        breakdown.append(
            {
                "tf": block.get("tf", "current"),
                "weight": round(w, 2),
                "score": round(s, 2),
                "trend": block.get("trend", ""),
                "macd_state": block.get("macd_state", ""),
                "rsi": block.get("rsi"),
            }
        )

    if max_total <= 0:
        normalised = 0.0
        direction = 0
    else:
        normalised = total / max_total  # in [-1, 1]
        if normalised > 0.05:
            direction = 1
        elif normalised < -0.05:
            direction = -1
        else:
            direction = 0
    score_0_100 = round(abs(normalised) * 100.0, 1)
    return {
        "score": score_0_100,
        "direction": direction,  # -1 / 0 / +1
        "label": _label_for(score_0_100, direction),
        "breakdown": breakdown,
    }


def alignment_summary_for_prompt(alignment: dict | None) -> str:
    if not alignment:
        return ""
    direction_word = (
        "вверх"
        if alignment.get("direction", 0) > 0
        else "вниз"
        if alignment.get("direction", 0) < 0
        else "—"
    )
    return (
        f"  • Multi-TF alignment: {alignment.get('score', 0)}/100 "
        f"({alignment.get('label', '')}, направление {direction_word})"
    )

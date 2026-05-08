"""LLM analysis layer.

Tries providers in order: Groq → Gemini → rules-based fallback. Each provider
is opt-in via its env var (GROQ_API_KEY, GEMINI_API_KEY); if none are
configured, the deterministic fallback keeps the app functional.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

import httpx

from .data import get_market_precision
from .patterns import patterns_summary_for_prompt
from .schemas import Analysis, Signal

log = logging.getLogger(__name__)

# Minimum acceptable risk:reward ratio. Below this we refuse to publish a
# trade idea (downgrade to flat) — taking 1:1 setups against fees+slippage is
# negative-expectation in the long run.
MIN_RR = 1.5

# Volatility cap: if ATR is more than this fraction of price the market is
# in a regime where stop placement is unreliable. We refuse a directional
# entry and let the user wait for things to calm down.
MAX_ATR_PCT = 8.0

SYSTEM_PROMPT = """Ты — опытный криптотрейдер и технический аналитик. Твоя задача:
1. Принять JSON со сводкой технических индикаторов и уровней.
2. Выдать структурированный анализ на русском языке.
3. Предложить торговую идею с конкретным уровнем входа, типом входа, стоп-лоссом и двумя тейк-профитами.
4. Указать риски и текущий режим рынка.

ТИПЫ ВХОДА (поле entry_type, обязательное):
- "market" — вход по рынку у текущей цены close. Используй, когда сигнал актуален «прямо сейчас» и нет смысла ждать. entry должен быть в пределах ±0.5*ATR от close.
- "limit" — пассивный лимитный ордер, ждём отката к уровню. Для long: entry СТРОГО ниже close (на 0.4–1.5 ATR), у поддержки. Для short: entry СТРОГО выше close (на 0.4–1.5 ATR), у сопротивления.
- "stop" — вход по пробою (стоп-ордер). Для long: entry СТРОГО выше close (на 0.1–1.0 ATR), за сопротивлением. Для short: entry СТРОГО ниже close (на 0.1–1.0 ATR), за поддержкой.
- При сомнениях ставь "market". Никогда не комбинируй типы (например, "limit" с entry выше close — ошибка, уйдёт в flat).

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА (нарушение → ставь direction = "flat"):
- entry должен соответствовать entry_type (см. выше). Не выдумывай уровни далеко от рынка.
- Для long: stop_loss < entry < take_profit_1 ≤ take_profit_2.
- Для short: stop_loss > entry > take_profit_1 ≥ take_profit_2.
- Stop-loss располагай за ближайшим уровнем (поддержки для long, сопротивления для short) с буфером 0.3-0.7 ATR.
- Risk:Reward до TP1 — не менее 1.5. Если по уровням 1.5 не получается — direction = "flat".
- Если ATR > 8% от цены — рынок слишком волатилен, direction = "flat".
- Если старшие ТФ (htf_trends) единогласно против предлагаемого направления — снижай confidence минимум на 15.
- Fear & Greed > 80 → не открывай long; < 20 → не открывай short (оба эти диапазона — крайности).
- ADX < 18 при попытке трендовой идеи — снижай confidence минимум на 10.
- confidence — целое 0..100. Если direction="flat" — confidence ≤ 35.
- Сильные разворотные паттерны у уровней (Engulfing, Morning/Evening Star, Hammer/Shooting Star у S/R, сила ≥2) — серьёзный аргумент. Doji/Spinning Top сами по себе — повод снизить уверенность, не основание для входа.
- Не используй markdown и эмодзи. Возвращай ТОЛЬКО валидный JSON по схеме.

Схема ответа:
{
  "market_regime": "строка: тренд / диапазон / разгон / коррекция и т.п.",
  "trend": "восходящий / нисходящий / боковой",
  "key_levels": {"support": [числа], "resistance": [числа]},
  "indicators_summary": {"rsi": "...", "macd": "...", "ema": "...", "bollinger": "..."},
  "signal": {
    "direction": "long" | "short" | "flat",
    "entry": число или null,
    "entry_type": "market" | "limit" | "stop",
    "stop_loss": число или null,
    "take_profit_1": число или null,
    "take_profit_2": число или null,
    "confidence": 0..100,
    "rationale": "краткое обоснование"
  },
  "narrative": "развёрнутый комментарий 3-6 предложений",
  "risks": ["список рисков"]
}

Пример валидной идеи (для иллюстрации формата, не копировать числа). Здесь market — close=63000, entry≈close:
{
  "market_regime": "тренд",
  "trend": "восходящий",
  "key_levels": {"support": [62000, 61200], "resistance": [64500, 66000]},
  "indicators_summary": {"rsi": "58 — нейтрально", "macd": "бычий-разгон", "ema": "20>50>200", "bollinger": "расширение"},
  "signal": {
    "direction": "long",
    "entry": 63000,
    "entry_type": "market",
    "stop_loss": 61700,
    "take_profit_1": 64500,
    "take_profit_2": 66000,
    "confidence": 62,
    "rationale": "Цена выше EMA200, MACD расширяется, рядом support 62000."
  },
  "narrative": "...",
  "risks": ["..."]
}

Пример лимитного входа (close=63500, support 62000 в 1 ATR ниже):
{
  "market_regime": "тренд",
  "trend": "восходящий",
  "key_levels": {"support": [62000, 60800], "resistance": [64500, 66000]},
  "indicators_summary": {...},
  "signal": {
    "direction": "long",
    "entry": 62200,
    "entry_type": "limit",
    "stop_loss": 61300,
    "take_profit_1": 64500,
    "take_profit_2": 66000,
    "confidence": 58,
    "rationale": "Тренд вверх, ждём откат к 62000 — лимит выгоднее рынка."
  },
  "narrative": "...",
  "risks": ["..."]
}

Пример отказа (нет согласованной картины):
{
  "market_regime": "диапазон",
  "trend": "боковой",
  "key_levels": {"support": [...], "resistance": [...]},
  "indicators_summary": {...},
  "signal": {
    "direction": "flat",
    "entry": null,
    "entry_type": "market",
    "stop_loss": null,
    "take_profit_1": null,
    "take_profit_2": null,
    "confidence": 25,
    "rationale": "ADX 14 — нет тренда; ждать выхода из диапазона."
  },
  "narrative": "...",
  "risks": ["..."]
}
"""


def _build_user_prompt(
    coin: str,
    timeframe: str,
    summary: dict,
    extra: dict | None = None,
) -> str:
    atr_pct = summary.get("atr_pct", 0.0)
    parts = [
        f"Монета: {coin}/USDT",
        f"Таймфрейм: {timeframe}",
        f"Текущая цена: {summary['close']}",
        f"ATR: {summary.get('atr', 0):.4f} ({atr_pct:.2f}% от цены)",
        f"ADX: {summary.get('adx', 0):.1f} ({summary.get('adx_state', '')})",
        f"BB: {summary.get('bb_state', '')} (ширина {summary.get('bb_width', 0):.4f})",
        "",
        f"Технические данные (JSON):\n{json.dumps(summary, ensure_ascii=False, indent=2)}",
    ]
    if extra:
        htf = extra.get("htf_trends") or []
        if htf:
            htf_lines = [
                f"  • {h['tf']}: тренд {h['trend']}, RSI {h['rsi']} ({h['rsi_state']}), MACD {h['macd_state']}, изм. за 30 баров {h['change_pct_30bars']}%"
                for h in htf
            ]
            parts.append("\nКонтекст старших ТФ:\n" + "\n".join(htf_lines))
        fng = extra.get("fear_greed")
        if fng:
            parts.append(
                f"\nИндекс страха и жадности: {fng['value']} ({fng['classification']})"
            )
        news = [t for t in (extra.get("news_titles") or []) if t]
        if news:
            parts.append("\nСвежие заголовки новостей:\n- " + "\n- ".join(news[:5]))
        patterns = extra.get("patterns") or []
        if patterns:
            ps = patterns_summary_for_prompt(patterns, limit=6)
            if ps:
                parts.append(
                    "\nСвежие свечные паттерны (от новых к старым, сила ≥2/3):\n"
                    + ps
                )
    parts.append(
        "\nИспользуй обязательные правила из system-prompt: проверь RR ≥ 1.5, направление SL/TP, ATR-фильтр,"
        " HTF-согласие и F&G. Если хоть одно условие не выполнено — direction = \"flat\" (даже если паттерн красивый)."
        " Сделай анализ и торговую идею. Ответь ТОЛЬКО JSON по указанной схеме."
    )
    return "\n".join(parts)


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # remove first fence
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _parse_analysis_json(text: str, coin: str, timeframe: str) -> Analysis:
    raw = _strip_code_fences(text)
    data: dict[str, Any] = json.loads(raw)
    sig_in = dict(data.get("signal", {}))
    sig_in.pop("rr", None)  # never trust the model on this — recomputed in validation
    signal = Signal(**sig_in)
    return Analysis(
        coin=coin,
        timeframe=timeframe,
        market_regime=data.get("market_regime", ""),
        trend=data.get("trend", ""),
        key_levels=data.get("key_levels", {"support": [], "resistance": []}),
        indicators_summary=data.get("indicators_summary", {}),
        signal=signal,
        narrative=data.get("narrative", ""),
        risks=data.get("risks", []),
    )


# ---------------------------------------------------------------------------
# Tick-size aware rounding & sanity-check helpers
# ---------------------------------------------------------------------------

def _decimals_from_precision(p: Any) -> int | None:
    """ccxt precision can be either decimal places (int) or tick size (float)."""
    if p is None:
        return None
    try:
        if isinstance(p, int):
            return max(0, min(int(p), 12))
        pf = float(p)
        if pf <= 0:
            return None
        if pf >= 1:
            # Likely decimal-places encoded as float
            return max(0, min(int(round(pf)), 12))
        # Tick size: count decimals
        d = max(0, int(round(-math.log10(pf))))
        return min(d, 12)
    except (ValueError, TypeError):
        return None


def _round_price(value: float | None, decimals: int) -> float | None:
    if value is None:
        return None
    return round(float(value), decimals)


def _decimals_for_close(close: float) -> int:
    """Default rounding when no exchange precision is available."""
    if close >= 1000:
        return 2
    if close >= 10:
        return 3
    if close >= 1:
        return 4
    return 6


def _compute_rr(direction: str, entry: float, stop: float, tp: float) -> float | None:
    if direction == "long":
        risk = entry - stop
        reward = tp - entry
    elif direction == "short":
        risk = stop - entry
        reward = entry - tp
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


# Maximum distance an entry may sit from the current close, expressed in
# multiples of ATR. Limit/stop entries are allowed to be further than market
# entries because they explicitly target a different price.
_ENTRY_DIST_ATR = {
    "market": 0.5,
    "limit": 1.5,
    "stop": 1.5,
}


def _entry_type_consistent(
    direction: str,
    entry_type: str,
    entry: float,
    close: float,
    atr_v: float,
) -> tuple[bool, str]:
    """Check that entry sits on the correct side of close for its type.

    Returns (ok, reason). When `atr_v <= 0` we only enforce the side
    relation, not the magnitude.
    """
    diff = entry - close
    max_dist = _ENTRY_DIST_ATR.get(entry_type, 0.5) * atr_v if atr_v > 0 else float("inf")
    if entry_type == "market":
        if atr_v > 0 and abs(diff) > max_dist:
            return False, "entry далеко от текущей цены для market-входа"
        return True, ""
    if direction == "long":
        if entry_type == "limit":
            if diff >= 0:
                return False, "limit-вход на лонге должен быть ниже close"
            if atr_v > 0 and -diff > max_dist:
                return False, "limit-вход слишком далеко ниже close"
            return True, ""
        if entry_type == "stop":
            if diff <= 0:
                return False, "stop-вход на лонге должен быть выше close (пробой вверх)"
            if atr_v > 0 and diff > max_dist:
                return False, "stop-вход слишком далеко выше close"
            return True, ""
    elif direction == "short":
        if entry_type == "limit":
            if diff <= 0:
                return False, "limit-вход на шорте должен быть выше close"
            if atr_v > 0 and diff > max_dist:
                return False, "limit-вход слишком далеко выше close"
            return True, ""
        if entry_type == "stop":
            if diff >= 0:
                return False, "stop-вход на шорте должен быть ниже close (пробой вниз)"
            if atr_v > 0 and -diff > max_dist:
                return False, "stop-вход слишком далеко ниже close"
            return True, ""
    return False, "неизвестный entry_type"


def _flatten(reason: str) -> dict:
    return {
        "direction": "flat",
        "entry": None,
        "stop_loss": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "confidence_cap": 35,
        "reason": reason,
    }


def _validate_signal(
    sig: Signal,
    summary: dict,
    extra: dict | None,
) -> Signal:
    """Final post-LLM gate.

    Enforces the constraints from SYSTEM_PROMPT in code so the user never
    gets a structurally broken trade idea even when the model hallucinates.
    """
    direction = sig.direction
    if direction == "flat":
        return Signal(
            direction="flat",
            entry=None,
            stop_loss=None,
            take_profit_1=None,
            take_profit_2=None,
            confidence=min(sig.confidence, 35),
            rationale=sig.rationale,
            rr=None,
        )

    entry = sig.entry
    stop = sig.stop_loss
    tp1 = sig.take_profit_1
    tp2 = sig.take_profit_2
    close = float(summary.get("close", 0.0))
    atr_v = float(summary.get("atr", 0.0))
    atr_pct = float(summary.get("atr_pct", 0.0))

    notes: list[str] = []
    if entry is None or stop is None or tp1 is None:
        flat = _flatten("неполные уровни сделки")
        return Signal(
            direction=flat["direction"],
            entry=flat["entry"],
            stop_loss=flat["stop_loss"],
            take_profit_1=flat["take_profit_1"],
            take_profit_2=flat["take_profit_2"],
            confidence=min(sig.confidence, flat["confidence_cap"]),
            rationale=(sig.rationale + " " + flat["reason"]).strip(),
            rr=None,
        )

    # ATR-pct sanity: refuse trade ideas in extreme volatility regimes.
    if atr_pct >= MAX_ATR_PCT:
        flat = _flatten(f"ATR {atr_pct:.1f}% — слишком волатильно для входа")
        return Signal(
            direction=flat["direction"],
            entry=flat["entry"],
            stop_loss=flat["stop_loss"],
            take_profit_1=flat["take_profit_1"],
            take_profit_2=flat["take_profit_2"],
            confidence=min(sig.confidence, flat["confidence_cap"]),
            rationale=(sig.rationale + " " + flat["reason"]).strip(),
            rr=None,
        )

    # Entry must sit on the correct side of close for its entry_type and
    # within a reasonable distance (C2). market = ±0.5 ATR; limit/stop = up
    # to 1.5 ATR away on the appropriate side.
    ok, reason = _entry_type_consistent(direction, sig.entry_type, entry, close, atr_v)
    if not ok:
        flat = _flatten(reason)
        return Signal(
            direction=flat["direction"],
            entry=flat["entry"],
            entry_type="market",
            stop_loss=flat["stop_loss"],
            take_profit_1=flat["take_profit_1"],
            take_profit_2=flat["take_profit_2"],
            confidence=min(sig.confidence, flat["confidence_cap"]),
            rationale=(sig.rationale + " " + flat["reason"]).strip(),
            rr=None,
        )

    # Stop / TP direction sanity.
    side_ok = (direction == "long" and stop < entry < tp1 and (tp2 is None or tp2 >= tp1)) or (
        direction == "short" and stop > entry > tp1 and (tp2 is None or tp2 <= tp1)
    )
    if not side_ok:
        flat = _flatten("SL/TP неконсистентны с направлением")
        return Signal(
            direction=flat["direction"],
            entry=flat["entry"],
            stop_loss=flat["stop_loss"],
            take_profit_1=flat["take_profit_1"],
            take_profit_2=flat["take_profit_2"],
            confidence=min(sig.confidence, flat["confidence_cap"]),
            rationale=(sig.rationale + " " + flat["reason"]).strip(),
            rr=None,
        )

    rr = _compute_rr(direction, entry, stop, tp1)
    if rr is None or rr < MIN_RR:
        flat = _flatten(f"RR {rr:.2f} < {MIN_RR}" if rr else "RR не определён")
        return Signal(
            direction=flat["direction"],
            entry=flat["entry"],
            stop_loss=flat["stop_loss"],
            take_profit_1=flat["take_profit_1"],
            take_profit_2=flat["take_profit_2"],
            confidence=min(sig.confidence, flat["confidence_cap"]),
            rationale=(sig.rationale + " " + flat["reason"]).strip(),
            rr=rr,
        )

    # HTF confidence penalty — when all of the higher TFs disagree.
    confidence = sig.confidence
    htf = (extra or {}).get("htf_trends") or []
    if htf:
        opposite_word = "нисход" if direction == "long" else "восход"
        agree_word = "восход" if direction == "long" else "нисход"
        opposed = sum(1 for h in htf if opposite_word in str(h.get("trend", "")))
        agreed = sum(1 for h in htf if agree_word in str(h.get("trend", "")))
        if opposed >= max(2, len(htf) - 1) and agreed == 0:
            confidence = max(20, confidence - 15)
            notes.append("HTF единогласно против — confidence снижен")

    fng = (extra or {}).get("fear_greed")
    if fng:
        try:
            value = int(fng.get("value", 50))
        except (TypeError, ValueError):
            value = 50
        if direction == "long" and value >= 80:
            confidence = max(20, confidence - 10)
            notes.append("F&G в зоне жадности — long с риском")
        elif direction == "short" and value <= 20:
            confidence = max(20, confidence - 10)
            notes.append("F&G в зоне страха — short с риском")

    adx_v = float(summary.get("adx", 0.0))
    if adx_v < 18:
        confidence = max(20, confidence - 10)
        notes.append("ADX слабый — трендовая идея с риском")

    rationale = sig.rationale
    if notes:
        rationale = (rationale + " " + "; ".join(notes)).strip()

    return Signal(
        direction=direction,
        entry=entry,
        entry_type=sig.entry_type,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        confidence=int(max(0, min(100, confidence))),
        rationale=rationale,
        rr=round(rr, 2),
    )


def _round_signal_levels(sig: Signal, coin: str, close: float) -> Signal:
    """Round entry/SL/TP to the exchange's tick precision (G4)."""
    if sig.direction == "flat":
        return sig
    precision = get_market_precision(coin)
    decimals = _decimals_from_precision(precision.get("price"))
    if decimals is None:
        decimals = _decimals_for_close(close)
    return sig.model_copy(
        update={
            "entry": _round_price(sig.entry, decimals),
            "stop_loss": _round_price(sig.stop_loss, decimals),
            "take_profit_1": _round_price(sig.take_profit_1, decimals),
            "take_profit_2": _round_price(sig.take_profit_2, decimals),
        }
    )


def _groq_analyze(
    coin: str,
    timeframe: str,
    summary: dict,
    extra: dict | None = None,
) -> Analysis | None:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(coin, timeframe, summary, extra)},
        ],
        # Low temperature: structured numeric output, we don't want creative
        # rephrasings of price levels between requests.
        "temperature": 0.1,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        text = data["choices"][0]["message"]["content"]
        return _parse_analysis_json(text, coin, timeframe)
    except Exception as e:  # noqa: BLE001
        log.warning("Groq analysis failed: %s", e)
        return None


def _gemini_analyze(
    coin: str,
    timeframe: str,
    summary: dict,
    extra: dict | None = None,
) -> Analysis | None:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            system_instruction=SYSTEM_PROMPT,
            generation_config={
                "temperature": 0.1,
                "top_p": 0.9,
                "max_output_tokens": 2048,
                "response_mime_type": "application/json",
            },
        )
        resp = model.generate_content(_build_user_prompt(coin, timeframe, summary, extra))
        text = (resp.text or "").strip()
        if not text:
            return None
        return _parse_analysis_json(text, coin, timeframe)
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini analysis failed: %s", e)
        return None


def _htf_disagreement(direction: str, htf: list[dict]) -> tuple[int, int]:
    """Return (#opposed, #agreed) higher TFs for the given direction."""
    if not htf or direction == "flat":
        return 0, 0
    opp = "нисход" if direction == "long" else "восход"
    agr = "восход" if direction == "long" else "нисход"
    opposed = sum(1 for h in htf if opp in str(h.get("trend", "")))
    agreed = sum(1 for h in htf if agr in str(h.get("trend", "")))
    return opposed, agreed


def _rules_based_fallback(
    coin: str,
    timeframe: str,
    summary: dict,
    extra: dict | None = None,
) -> Analysis:
    close = float(summary["close"])
    atr_v = float(summary["atr"])
    atr_pct = float(summary.get("atr_pct", 0.0))
    trend = summary["trend"]
    rsi_v = float(summary["rsi"])
    macd_state = summary["macd_state"]
    adx_v = float(summary.get("adx", 0.0))
    support = sorted([s for s in summary["support"] if s < close], reverse=True)
    resistance = sorted([r for r in summary["resistance"] if r > close])

    direction = "flat"
    entry: float | None = None
    entry_type: str = "market"
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    confidence = 30
    rationale_parts: list[str] = []

    patterns = (extra or {}).get("patterns") or []
    fresh = [p for p in patterns if p.get("bar_index", -99) >= -3]
    bull_score = sum(p["strength"] for p in fresh if p.get("bias") == "bullish")
    bear_score = sum(p["strength"] for p in fresh if p.get("bias") == "bearish")
    indecision = any(p.get("kind") == "indecision" for p in fresh)

    bullish = trend == "восходящий" and macd_state in {"бычий", "бычий-разгон", "разворот вверх", "нейтрально"} and rsi_v < 72
    bearish = trend == "нисходящий" and macd_state in {"медвежий", "медвежий-разгон", "разворот вниз", "нейтрально"} and rsi_v > 28

    # ATR-pct sanity gate (C7) — skip the directional logic entirely when
    # volatility is in a regime where stop placement is unreliable.
    if atr_pct >= MAX_ATR_PCT:
        rationale_parts.append(
            f"ATR {atr_pct:.1f}% — слишком высокая волатильность для входа."
        )
    elif bullish and support and resistance:
        direction = "long"
        confidence = 55
        dist_to_resistance = resistance[0] - close
        dist_to_support = close - support[0]
        if 0 < dist_to_resistance <= 0.4 * atr_v and len(resistance) > 1:
            # Price right under a key resistance — wait for breakout (C2 stop entry).
            entry_type = "stop"
            entry = resistance[0] + 0.1 * atr_v
            stop = resistance[0] - 0.4 * atr_v
            if stop > entry - 0.8 * atr_v:
                stop = entry - 0.8 * atr_v
            tp1 = resistance[1]
            tp2 = resistance[2] if len(resistance) > 2 else None
            rationale_parts.append(
                f"Лонг по пробою сопротивления {resistance[0]:.4f} (stop-вход)."
            )
        elif 0.6 * atr_v < dist_to_support <= 1.5 * atr_v:
            # Trend is up but price is mid-range — passively wait for pullback (C2 limit entry).
            entry_type = "limit"
            entry = support[0] + 0.2 * atr_v
            stop = support[0] - 0.5 * atr_v
            if stop > entry - 0.8 * atr_v:
                stop = entry - 0.8 * atr_v
            tp1 = resistance[0]
            tp2 = resistance[1] if len(resistance) > 1 else None
            rationale_parts.append(
                f"Лонг лимитом на откате к {support[0]:.4f}."
            )
        else:
            entry_type = "market"
            entry = close
            cand_a = support[0] - atr_v * 0.5
            cand_b = close - atr_v * 1.0
            stop = max(cand_a, cand_b)
            if stop > close - atr_v * 0.8:
                stop = close - atr_v * 0.8
            tp1 = resistance[0]
            tp2 = resistance[1] if len(resistance) > 1 else None
            rationale_parts.append("EMA-стек вверх, MACD не разворачивается, RSI без перегрева.")
    elif bearish and support and resistance:
        direction = "short"
        confidence = 55
        dist_to_support = close - support[0]
        dist_to_resistance = resistance[0] - close
        if 0 < dist_to_support <= 0.4 * atr_v and len(support) > 1:
            # Price right above a key support — wait for breakdown (C2 stop entry).
            entry_type = "stop"
            entry = support[0] - 0.1 * atr_v
            stop = support[0] + 0.4 * atr_v
            if stop < entry + 0.8 * atr_v:
                stop = entry + 0.8 * atr_v
            tp1 = support[1]
            tp2 = support[2] if len(support) > 2 else None
            rationale_parts.append(
                f"Шорт по пробою поддержки {support[0]:.4f} (stop-вход)."
            )
        elif 0.6 * atr_v < dist_to_resistance <= 1.5 * atr_v:
            # Trend is down but price is mid-range — passively wait for retest (C2 limit entry).
            entry_type = "limit"
            entry = resistance[0] - 0.2 * atr_v
            stop = resistance[0] + 0.5 * atr_v
            if stop < entry + 0.8 * atr_v:
                stop = entry + 0.8 * atr_v
            tp1 = support[0]
            tp2 = support[1] if len(support) > 1 else None
            rationale_parts.append(
                f"Шорт лимитом на ретесте {resistance[0]:.4f}."
            )
        else:
            entry_type = "market"
            entry = close
            cand_a = resistance[0] + atr_v * 0.5
            cand_b = close + atr_v * 1.0
            stop = min(cand_a, cand_b)
            if stop < close + atr_v * 0.8:
                stop = close + atr_v * 0.8
            tp1 = support[0]
            tp2 = support[1] if len(support) > 1 else None
            rationale_parts.append("EMA-стек вниз, MACD не разворачивается, RSI не в перепроданности.")
    else:
        rationale_parts.append("Нет согласованных сигналов — ждём подтверждения от уровней.")

    # HTF agreement gate (C5) — the deterministic path now sees the same
    # higher-TF context the model does.
    htf = (extra or {}).get("htf_trends") or []
    if direction != "flat":
        opposed, agreed = _htf_disagreement(direction, htf)
        if htf and opposed >= max(2, len(htf) - 1) and agreed == 0:
            rationale_parts.append("Старшие ТФ единогласно против — отказ от идеи.")
            direction = "flat"
            entry = stop = tp1 = tp2 = None
            confidence = min(confidence, 30)

    # Fear & Greed extreme gate (C6).
    fng = (extra or {}).get("fear_greed")
    if fng and direction != "flat":
        try:
            value = int(fng.get("value", 50))
        except (TypeError, ValueError):
            value = 50
        if direction == "long" and value >= 80:
            confidence = max(20, confidence - 10)
            rationale_parts.append("F&G в зоне жадности — лонг рискован.")
        elif direction == "short" and value <= 20:
            confidence = max(20, confidence - 10)
            rationale_parts.append("F&G в зоне страха — шорт рискован.")

    # Pattern adjustment
    if direction == "long" and bull_score >= 3:
        confidence = min(80, confidence + 12)
        rationale_parts.append("Свежие бычьи паттерны подтверждают идею.")
    elif direction == "long" and bear_score >= 3:
        confidence = max(20, confidence - 15)
        rationale_parts.append("Свежие медвежьи паттерны ослабляют идею лонга.")
    elif direction == "short" and bear_score >= 3:
        confidence = min(80, confidence + 12)
        rationale_parts.append("Свежие медвежьи паттерны подтверждают идею.")
    elif direction == "short" and bull_score >= 3:
        confidence = max(20, confidence - 15)
        rationale_parts.append("Свежие бычьи паттерны ослабляют идею шорта.")
    if indecision and direction != "flat":
        confidence = max(20, confidence - 5)

    # ADX trend-strength penalty (B4).
    if direction != "flat" and adx_v < 18:
        confidence = max(20, confidence - 10)
        rationale_parts.append(f"ADX {adx_v:.0f} — тренд слабый.")

    indicators_summary = {
        "rsi": f"{rsi_v:.1f} ({summary['rsi_state']})",
        "macd": f"{summary['macd']:.4f} / сигнал {summary['macd_signal']:.4f} ({macd_state})",
        "ema": f"20={summary['ema_fast']:.2f}, 50={summary['ema_slow']:.2f}, 200={summary['ema_long']:.2f}",
        "bollinger": f"низ={summary['bb_lower']:.2f}, верх={summary['bb_upper']:.2f} ({summary.get('bb_state', '')})",
        "adx": f"{adx_v:.1f} ({summary.get('adx_state', '')})",
    }

    narrative_parts = [
        f"Тренд по EMA: {trend}. RSI {rsi_v:.1f} — {summary['rsi_state']}. MACD {macd_state}.",
        f"Ближайшее сопротивление {resistance[0] if resistance else '—'},"
        f" ближайшая поддержка {support[0] if support else '—'}.",
        f"ATR {atr_v:.2f} ({atr_pct:.2f}% от цены), ADX {adx_v:.1f} — учитывайте при размере позиции.",
    ]
    if fresh:
        top = fresh[0]
        bias_ru = {"bullish": "бычий", "bearish": "медвежий", "neutral": "нейтр."}.get(
            top["bias"], top["bias"]
        )
        narrative_parts.append(
            f"Свежий паттерн: {top['name_ru']} ({bias_ru}, сила {top['strength']}/3) — {top['context']}."
        )
    narrative = " ".join(narrative_parts)

    risks = [
        "Высокая волатильность крипторынка — возможны резкие движения вне ТА",
        "Внешние новости и макроэкономика могут перекрыть техническую картину",
        "Ликвидность в стакане может отличаться от исторических объёмов",
    ]

    raw_signal = Signal(
        direction=direction,
        entry=entry,
        entry_type=entry_type,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        confidence=confidence,
        rationale=" ".join(rationale_parts),
    )
    # Run the same RR / level / consistency checks the LLM output is run
    # through (C1, C3, E1) and round to exchange tick precision (G4).
    validated = _validate_signal(raw_signal, summary, extra)
    rounded = _round_signal_levels(validated, coin, close)

    return Analysis(
        coin=coin,
        timeframe=timeframe,
        market_regime=trend,
        trend=trend,
        key_levels={"support": support[:3], "resistance": resistance[:3]},
        indicators_summary=indicators_summary,
        signal=rounded,
        narrative=narrative,
        risks=risks,
    )


def analyze(
    coin: str,
    timeframe: str,
    summary: dict,
    extra_context: dict | None = None,
) -> Analysis:
    for provider in (_groq_analyze, _gemini_analyze):
        result = provider(coin, timeframe, summary, extra_context)
        if result is not None:
            # Post-LLM validation + tick-aware rounding.
            close = float(summary.get("close", 0.0))
            result.signal = _round_signal_levels(
                _validate_signal(result.signal, summary, extra_context),
                coin,
                close,
            )
            return result
    return _rules_based_fallback(coin, timeframe, summary, extra_context)

"""LLM analysis layer.

Tries providers in order: Groq → Gemini → rules-based fallback. Each provider
is opt-in via its env var (GROQ_API_KEY, GEMINI_API_KEY); if none are
configured, the deterministic fallback keeps the app functional.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from .schemas import Analysis, Signal

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — опытный криптотрейдер и технический аналитик. Твоя задача:
1. Принять JSON со сводкой технических индикаторов и уровней.
2. Выдать структурированный анализ на русском языке.
3. Предложить торговую идею с конкретным уровнем входа, стоп-лоссом и двумя тейк-профитами.
4. Указать риски и текущий режим рынка.

ВАЖНО:
- Используй уровни поддержки/сопротивления из входных данных.
- Stop-loss всегда должен быть за ближайшим уровнем поддержки (для long) или сопротивления (для short).
- Take-profit ставь у следующих уровней.
- Если рынок неопределённый — выбирай direction = "flat" и не предлагай вход.
- confidence — целое от 0 до 100, отражает уверенность в идее.
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
    "stop_loss": число или null,
    "take_profit_1": число или null,
    "take_profit_2": число или null,
    "confidence": 0..100,
    "rationale": "краткое обоснование"
  },
  "narrative": "развёрнутый комментарий 3-6 предложений",
  "risks": ["список рисков"]
}
"""


def _build_user_prompt(coin: str, timeframe: str, summary: dict) -> str:
    return (
        f"Монета: {coin}/USDT\n"
        f"Таймфрейм: {timeframe}\n"
        f"Текущая цена: {summary['close']}\n\n"
        f"Технические данные (JSON):\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
        f"Сделай анализ и торговую идею. Ответь ТОЛЬКО JSON по указанной схеме."
    )


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
    signal = Signal(**data.get("signal", {}))
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


def _groq_analyze(coin: str, timeframe: str, summary: dict) -> Analysis | None:
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
            {"role": "user", "content": _build_user_prompt(coin, timeframe, summary)},
        ],
        "temperature": 0.4,
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


def _gemini_analyze(coin: str, timeframe: str, summary: dict) -> Analysis | None:
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
                "temperature": 0.4,
                "top_p": 0.9,
                "max_output_tokens": 2048,
                "response_mime_type": "application/json",
            },
        )
        resp = model.generate_content(_build_user_prompt(coin, timeframe, summary))
        text = (resp.text or "").strip()
        if not text:
            return None
        return _parse_analysis_json(text, coin, timeframe)
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini analysis failed: %s", e)
        return None


def _rules_based_fallback(coin: str, timeframe: str, summary: dict) -> Analysis:
    close = float(summary["close"])
    atr_v = float(summary["atr"])
    trend = summary["trend"]
    rsi_v = float(summary["rsi"])
    macd_state = summary["macd_state"]
    support = sorted([s for s in summary["support"] if s < close], reverse=True)
    resistance = sorted([r for r in summary["resistance"] if r > close])

    direction = "flat"
    entry = stop = tp1 = tp2 = None
    confidence = 30
    rationale_parts: list[str] = []

    bullish = trend == "восходящий" and macd_state in {"бычий", "нейтрально"} and rsi_v < 70
    bearish = trend == "нисходящий" and macd_state in {"медвежий", "нейтрально"} and rsi_v > 30

    if bullish and support and resistance:
        direction = "long"
        entry = round(close, 6)
        stop = round(min(support[0] - atr_v * 0.5, close - atr_v * 1.2), 6)
        tp1 = round(resistance[0], 6) if resistance else None
        tp2 = round(resistance[1], 6) if len(resistance) > 1 else None
        confidence = 55
        rationale_parts.append("EMA-стек указывает вверх, MACD не разворачивается, RSI без перегрева.")
    elif bearish and support and resistance:
        direction = "short"
        entry = round(close, 6)
        stop = round(max(resistance[0] + atr_v * 0.5, close + atr_v * 1.2), 6)
        tp1 = round(support[0], 6) if support else None
        tp2 = round(support[1], 6) if len(support) > 1 else None
        confidence = 55
        rationale_parts.append("EMA-стек указывает вниз, MACD не разворачивается, RSI не в перепроданности.")
    else:
        rationale_parts.append("Нет согласованных сигналов — ждём подтверждения от уровней.")

    indicators_summary = {
        "rsi": f"{rsi_v:.1f} ({summary['rsi_state']})",
        "macd": f"{summary['macd']:.4f} / сигнал {summary['macd_signal']:.4f} ({macd_state})",
        "ema": f"20={summary['ema_fast']:.2f}, 50={summary['ema_slow']:.2f}, 200={summary['ema_long']:.2f}",
        "bollinger": f"низ={summary['bb_lower']:.2f}, верх={summary['bb_upper']:.2f}",
    }

    narrative = (
        f"Тренд по EMA: {trend}. RSI {rsi_v:.1f} — {summary['rsi_state']}. MACD {macd_state}. "
        f"Ближайшее сопротивление {resistance[0] if resistance else '—'}, "
        f"ближайшая поддержка {support[0] if support else '—'}. "
        f"ATR {atr_v:.2f} — учитывайте при размере позиции и стопе."
    )

    risks = [
        "Высокая волатильность крипторынка — возможны резкие движения вне ТА",
        "Внешние новости и макроэкономика могут перекрыть техническую картину",
        "Ликвидность в стакане может отличаться от исторических объёмов",
    ]

    return Analysis(
        coin=coin,
        timeframe=timeframe,
        market_regime=trend,
        trend=trend,
        key_levels={"support": support[:3], "resistance": resistance[:3]},
        indicators_summary=indicators_summary,
        signal=Signal(
            direction=direction,
            entry=entry,
            stop_loss=stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            confidence=confidence,
            rationale=" ".join(rationale_parts),
        ),
        narrative=narrative,
        risks=risks,
    )


def analyze(coin: str, timeframe: str, summary: dict) -> Analysis:
    for provider in (_groq_analyze, _gemini_analyze):
        result = provider(coin, timeframe, summary)
        if result is not None:
            return result
    return _rules_based_fallback(coin, timeframe, summary)

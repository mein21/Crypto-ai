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

from .alignment import alignment_summary_for_prompt
from .backtest import backtest_summary_for_prompt
from .order_flow import order_flow_summary_for_prompt
from .patterns import patterns_summary_for_prompt
from .schemas import Analysis, Signal
from .sentiment import sentiment_summary_for_prompt
from .volume_profile import volume_profile_summary_for_prompt

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — опытный криптотрейдер и технический аналитик. Твоя задача:
1. Принять JSON со сводкой технических индикаторов и уровней.
2. Выдать структурированный анализ на русском языке.
3. Предложить торговую идею с конкретным уровнем входа, стоп-лоссом и двумя тейк-профитами.
4. Указать риски и текущий режим рынка.

ВАЖНО:
- Используй уровни поддержки/сопротивления из входных данных.
- Stop-loss всегда должен быть за ближайшим уровнем поддержки (для long) или сопротивления (для short).
- Take-profit-1 должен давать соотношение риск/прибыль (RR) ≈ 1.5: |TP1 − entry| / |entry − SL| ≈ 1.5 (допустимо 1.3–1.7). Если ближайший уровень даёт RR заметно меньше — отодвинь TP1 дальше; если сильно больше — выбирай ближе.
- Take-profit-2 ставь дальше TP1 с RR ≈ 2.5 (допустимо 2.0–3.5).
- Если рынок неопределённый — выбирай direction = "flat" и не предлагай вход.
- confidence — целое от 0 до 100, отражает уверенность в идее.
- Если переданы свежие свечные паттерны — обязательно упомяни их в narrative и учти при выборе direction/confidence: сильные разворотные паттерны у уровней (Bullish/Bearish Engulfing, Morning/Evening Star, Hammer/Shooting Star у S/R) — серьёзный аргумент; продолжающие паттерны (Marubozu, Three White Soldiers) подтверждают тренд; Doji/Spinning Top — повод снизить уверенность.
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


def _build_user_prompt(
    coin: str,
    timeframe: str,
    summary: dict,
    extra: dict | None = None,
) -> str:
    parts = [
        f"Монета: {coin}/USDT",
        f"Таймфрейм: {timeframe}",
        f"Текущая цена: {summary['close']}",
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
        onchain = extra.get("onchain") or {}
        coin = extra.get("coin", "")
        if onchain and coin == "BTC":
            fees = onchain.get("fees_sat_per_vb", {})
            parts.append(
                "\nОн-чейн (Bitcoin):\n"
                f"  • Комиссии sat/vB: fastest {fees.get('fastest')}, halfHour {fees.get('half_hour')}, hour {fees.get('hour')}, economy {fees.get('economy')}\n"
                f"  • Мемпул: {onchain.get('mempool_count')} tx, {onchain.get('mempool_vsize_mb')} MB\n"
                f"  • Хэшрейт: {onchain.get('hashrate_eh')} EH/s\n"
                f"  • Прогресс эпохи сложности: {onchain.get('difficulty_progress_pct')}% (расчётное изменение {onchain.get('difficulty_change_pct')}%)\n"
                f"  • Высота блока: {onchain.get('block_height')}"
            )
        if onchain and coin == "ETH":
            gas = onchain.get("gas_gwei", {})
            parts.append(
                "\nОн-чейн (Ethereum):\n"
                f"  • Газ gwei: slow {gas.get('slow')}, standard {gas.get('standard')}, fast {gas.get('fast')}\n"
                f"  • Base fee: {onchain.get('base_fee_gwei')} gwei\n"
                f"  • Загрузка блоков (10 блоков): {onchain.get('congestion_pct')}%\n"
                f"  • Блок: {onchain.get('block_number')}"
            )
        patterns = extra.get("patterns") or []
        if patterns:
            ps = patterns_summary_for_prompt(patterns, limit=6)
            if ps:
                parts.append(
                    "\nСвежие свечные паттерны (от новых к старым, последние 10 баров):\n"
                    + ps
                )
        vp = extra.get("volume_profile")
        if vp:
            vps = volume_profile_summary_for_prompt(vp)
            if vps:
                parts.append("\nVolume profile:\n" + vps)
        flow = extra.get("order_flow")
        if flow:
            fs = order_flow_summary_for_prompt(flow)
            if fs:
                parts.append("\nOrder flow (CVD):\n" + fs)
        alignment = extra.get("alignment")
        if alignment:
            als = alignment_summary_for_prompt(alignment)
            if als:
                parts.append("\nMulti-TF alignment:\n" + als)
        sentiment = extra.get("sentiment")
        if sentiment:
            ss = sentiment_summary_for_prompt(sentiment)
            if ss:
                parts.append("\nSentiment:\n" + ss)
        strategy = extra.get("strategy_stats")
        if strategy:
            bs = backtest_summary_for_prompt(strategy)
            if bs:
                parts.append("\nWalk-forward (rules):\n" + bs)
    parts.append(
        "\nУчти контекст старших ТФ (если они идут против анализируемого ТФ — снижай уверенность),"
        " настроение рынка (F&G < 25 — экстремальный страх, > 75 — жадность), заголовки новостей,"
        " он-чейн метрики (для BTC: высокие комиссии и забитый мемпул — признак ажиотажа; низкие — спокойствия;"
        " для ETH: газ выше 50 gwei — высокий спрос, ниже 15 — затишье), свежие свечные паттерны,"
        " профиль объёма (POC/VAH/VAL — ключевые магнитные уровни), CVD-дивергенции,"
        " multi-TF alignment (если score < 25 — снижай confidence), композитный sentiment"
        " и историческую статистику стратегии (winrate / PF). Упомяни существенные факторы в narrative."
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
                "temperature": 0.4,
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


TP1_RR_TARGET = 1.5
TP2_RR_TARGET = 2.5
TP1_RR_BAND = (1.3, 1.7)
TP2_RR_BAND = (2.0, 3.5)


def _round_price(value: float, ref: float) -> float:
    """Round price to a sensible precision based on magnitude (crypto prices vary 6+ orders)."""
    if ref >= 1000:
        return round(value, 2)
    if ref >= 10:
        return round(value, 4)
    return round(value, 6)


def _enforce_rr_targets(signal: Signal) -> Signal:
    """Post-process LLM/rules signal so TP1 lands inside RR band [1.3, 1.7] and TP2 inside [2.0, 3.5].

    If the LLM returned values outside the band (or missing TP2), we recompute relative to entry+stop
    so the user always sees a trade idea with a sane reward/risk profile.
    """
    if signal.direction not in {"long", "short"}:
        return signal
    entry = signal.entry
    stop = signal.stop_loss
    if entry is None or stop is None:
        return signal
    risk = abs(entry - stop)
    if risk <= 0:
        return signal
    sign = 1 if signal.direction == "long" else -1

    def rr(target: float | None) -> float | None:
        if target is None:
            return None
        return (target - entry) / risk * sign

    tp1_rr = rr(signal.take_profit_1)
    if tp1_rr is None or not (TP1_RR_BAND[0] <= tp1_rr <= TP1_RR_BAND[1]):
        signal.take_profit_1 = _round_price(entry + sign * risk * TP1_RR_TARGET, entry)

    tp2_rr = rr(signal.take_profit_2)
    needs_tp2 = (
        tp2_rr is None
        or tp2_rr <= TP1_RR_TARGET
        or not (TP2_RR_BAND[0] <= tp2_rr <= TP2_RR_BAND[1])
    )
    if needs_tp2:
        signal.take_profit_2 = _round_price(entry + sign * risk * TP2_RR_TARGET, entry)
    return signal


def _rules_based_fallback(
    coin: str,
    timeframe: str,
    summary: dict,
    extra: dict | None = None,
) -> Analysis:
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

    patterns = (extra or {}).get("patterns") or []
    fresh = [p for p in patterns if p.get("bar_index", -99) >= -3]
    bull_score = sum(p["strength"] for p in fresh if p.get("bias") == "bullish")
    bear_score = sum(p["strength"] for p in fresh if p.get("bias") == "bearish")
    indecision = any(p.get("kind") == "indecision" for p in fresh)

    bb_lower = float(summary["bb_lower"])
    bb_upper = float(summary["bb_upper"])

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

    # --- Confidence boosters (multi-factor agreement) -------------------------
    if direction == "long":
        if macd_state == "бычий":
            confidence += 5
            rationale_parts.append("MACD подтверждает бычий сигнал.")
        if rsi_v < 45:
            confidence += 5
            rationale_parts.append("RSI в зоне роста (ниже 45).")
        if close <= bb_lower + (bb_upper - bb_lower) * 0.25:
            confidence += 5
            rationale_parts.append("Цена у нижней Боллинджера — потенциал отскока.")
    elif direction == "short":
        if macd_state == "медвежий":
            confidence += 5
            rationale_parts.append("MACD подтверждает медвежий сигнал.")
        if rsi_v > 55:
            confidence += 5
            rationale_parts.append("RSI в зоне снижения (выше 55).")
        if close >= bb_lower + (bb_upper - bb_lower) * 0.75:
            confidence += 5
            rationale_parts.append("Цена у верхней Боллинджера — потенциал отката.")

    # Pattern adjustment
    if direction == "long" and bull_score >= 3:
        confidence += 12
        rationale_parts.append("Свежие бычьи паттерны подтверждают идею.")
    elif direction == "long" and bear_score >= 3:
        confidence = max(20, confidence - 15)
        rationale_parts.append("Свежие медвежьи паттерны ослабляют идею лонга.")
    elif direction == "short" and bear_score >= 3:
        confidence += 12
        rationale_parts.append("Свежие медвежьи паттерны подтверждают идею.")
    elif direction == "short" and bull_score >= 3:
        confidence = max(20, confidence - 15)
        rationale_parts.append("Свежие бычьи паттерны ослабляют идею шорта.")
    if indecision and direction != "flat":
        confidence = max(20, confidence - 5)

    # Order flow / CVD divergence
    flow = (extra or {}).get("order_flow") or {}
    div = flow.get("divergence", "none")
    if direction == "long" and div == "bullish":
        confidence = min(85, confidence + 8)
        rationale_parts.append("Бычья CVD-дивергенция подтверждает покупателей.")
    elif direction == "long" and div == "bearish":
        confidence = max(15, confidence - 12)
        rationale_parts.append("Медвежья CVD-дивергенция: на росте нет объёма.")
    elif direction == "short" and div == "bearish":
        confidence = min(85, confidence + 8)
        rationale_parts.append("Медвежья CVD-дивергенция подтверждает продавцов.")
    elif direction == "short" and div == "bullish":
        confidence = max(15, confidence - 12)
        rationale_parts.append("Бычья CVD-дивергенция: покупатели абсорбируют слив.")

    # Volume profile — penalty for trading against the value area
    vp = (extra or {}).get("volume_profile") or {}
    pos = vp.get("position")
    if direction == "long" and pos == "below_va":
        confidence = max(15, confidence - 8)
        rationale_parts.append("Цена ниже зоны стоимости — лонг идёт против контекста профиля.")
    elif direction == "short" and pos == "above_va":
        confidence = max(15, confidence - 8)
        rationale_parts.append("Цена выше зоны стоимости — шорт идёт против контекста профиля.")

    # Multi-TF alignment
    alignment = (extra or {}).get("alignment") or {}
    align_dir = alignment.get("direction", 0)
    align_score = float(alignment.get("score", 0) or 0)
    if direction == "long" and align_dir > 0 and align_score >= 50:
        confidence = min(90, confidence + 10)
        rationale_parts.append("Старшие ТФ согласованы вверх.")
    elif direction == "long" and align_dir < 0 and align_score >= 50:
        confidence = max(15, confidence - 12)
        rationale_parts.append("Старшие ТФ согласованы вниз — лонг против тренда.")
    elif direction == "short" and align_dir < 0 and align_score >= 50:
        confidence = min(90, confidence + 10)
        rationale_parts.append("Старшие ТФ согласованы вниз.")
    elif direction == "short" and align_dir > 0 and align_score >= 50:
        confidence = max(15, confidence - 12)
        rationale_parts.append("Старшие ТФ согласованы вверх — шорт против тренда.")

    # Composite sentiment — extreme readings nudge confidence
    sentiment = (extra or {}).get("sentiment") or {}
    sent_score = float(sentiment.get("score", 50) or 50)
    if direction == "long" and sent_score <= 25:
        confidence = max(15, confidence - 5)
        rationale_parts.append("Экстремальный медвежий sentiment ослабляет лонг.")
    elif direction == "short" and sent_score >= 75:
        confidence = max(15, confidence - 5)
        rationale_parts.append("Экстремальный бычий sentiment ослабляет шорт.")

    # Walk-forward backtest realism check
    strategy = (extra or {}).get("strategy_stats") or {}
    pf = float(strategy.get("profit_factor", 0) or 0)
    total_trades = int(strategy.get("total_trades", 0) or 0)
    if direction != "flat" and total_trades >= 5 and pf < 0.7:
        confidence = max(15, confidence - 8)
        rationale_parts.append(
            f"Историческая стратегия слабо работала (PF {pf}, сделок {total_trades})."
        )

    confidence = min(90, confidence)

    indicators_summary = {
        "rsi": f"{rsi_v:.1f} ({summary['rsi_state']})",
        "macd": f"{summary['macd']:.4f} / сигнал {summary['macd_signal']:.4f} ({macd_state})",
        "ema": f"20={summary['ema_fast']:.2f}, 50={summary['ema_slow']:.2f}, 200={summary['ema_long']:.2f}",
        "bollinger": f"низ={summary['bb_lower']:.2f}, верх={summary['bb_upper']:.2f}",
    }

    narrative_parts = [
        f"Тренд по EMA: {trend}. RSI {rsi_v:.1f} — {summary['rsi_state']}. MACD {macd_state}.",
        f"Ближайшее сопротивление {resistance[0] if resistance else '—'},"
        f" ближайшая поддержка {support[0] if support else '—'}.",
        f"ATR {atr_v:.2f} — учитывайте при размере позиции и стопе.",
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


def analyze(
    coin: str,
    timeframe: str,
    summary: dict,
    extra_context: dict | None = None,
) -> Analysis:
    for provider in (_groq_analyze, _gemini_analyze):
        result = provider(coin, timeframe, summary, extra_context)
        if result is not None:
            result.signal = _enforce_rr_targets(result.signal)
            return result
    fallback = _rules_based_fallback(coin, timeframe, summary, extra_context)
    fallback.signal = _enforce_rr_targets(fallback.signal)
    return fallback


def analyze_fast(
    coin: str,
    timeframe: str,
    summary: dict,
    extra_context: dict | None = None,
) -> Analysis:
    """Rules-based analysis only (no LLM). Used by cron to fit Vercel 10s timeout."""
    fallback = _rules_based_fallback(coin, timeframe, summary, extra_context)
    fallback.signal = _enforce_rr_targets(fallback.signal)
    return fallback

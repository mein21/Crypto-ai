"""Telegram bot webhook handler with interactive inline-keyboard buttons."""
from __future__ import annotations

import json
import logging
import os

import httpx

from .data import SYMBOL_MAP, fetch_ohlcv
from .indicators import compute_all
from .llm import analyze_fast
from .schemas import BestDealItem
from .telegram_notify import (
    HOLDING_PERIOD,
    format_deal_message,
    is_high_quality_deal,
)

log = logging.getLogger("crypto-ai.tg-bot")

TELEGRAM_API = "https://api.telegram.org"

TIMEFRAMES = ["15m", "1h", "4h", "1d", "1w"]
CRON_COINS = ["BTC", "ETH", "SOL", "BNB", "XRP"]


def _token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None


def _api(method: str, payload: dict) -> dict | None:
    token = _token()
    if not token:
        return None
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        return resp.json()
    except Exception as exc:
        log.warning("Telegram API %s failed: %s", method, exc)
        return None


def _send(chat_id: int | str, text: str, reply_markup: dict | None = None) -> dict | None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _api("sendMessage", payload)


def _edit(chat_id: int | str, message_id: int, text: str, reply_markup: dict | None = None) -> dict | None:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _api("editMessageText", payload)


def _answer_callback(callback_query_id: str, text: str = "") -> dict | None:
    return _api("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


# --- Keyboards ----------------------------------------------------------------

def _main_menu_kb() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f514 Проверить сигналы", "callback_data": "action_signals"},
                {"text": "\U0001f3c6 Лучшая сделка", "callback_data": "action_best"},
            ],
            [
                {"text": "\U0001f4ca Таймфреймы", "callback_data": "action_timeframes_info"},
                {"text": "\U0001f441 Отслеживания", "callback_data": "action_watches"},
            ],
        ]
    }


def _timeframe_kb(prefix: str) -> dict:
    row1 = [{"text": tf, "callback_data": f"{prefix}_{tf}"} for tf in TIMEFRAMES[:3]]
    row2 = [{"text": tf, "callback_data": f"{prefix}_{tf}"} for tf in TIMEFRAMES[3:]]
    return {"inline_keyboard": [row1, row2, [{"text": "\u25c0 Назад", "callback_data": "back_main"}]]}


# --- Scan logic (fast, rules-based) -------------------------------------------

def _scan_coins(timeframe: str, coins: list[str] | None = None) -> list[BestDealItem]:
    coin_list = coins or CRON_COINS
    items: list[BestDealItem] = []
    for coin in coin_list:
        try:
            df = fetch_ohlcv(coin, timeframe)
            if len(df) < 60:
                continue
            ind = compute_all(df)
            summary = ind.summary()
            result = analyze_fast(coin, timeframe, summary)
            sig = result.signal
            items.append(
                BestDealItem(
                    coin=coin,
                    timeframe=timeframe,
                    direction=sig.direction,
                    confidence=sig.confidence,
                    entry=sig.entry,
                    stop_loss=sig.stop_loss,
                    take_profit_1=sig.take_profit_1,
                    take_profit_2=sig.take_profit_2,
                    rationale=sig.rationale,
                    last_price=float(summary["close"]),
                    trend=result.trend,
                    rsi=round(float(summary["rsi"]), 1),
                    market_regime=result.market_regime,
                )
            )
        except Exception as exc:
            log.warning("Bot scan %s/%s failed: %s", coin, timeframe, exc)
    items.sort(key=lambda x: (x.direction != "flat", x.confidence), reverse=True)
    return items


# --- Command / callback handlers ---------------------------------------------

def _handle_start(chat_id: int | str) -> None:
    text = (
        "*Crypto AI Analyzer Bot*\n\n"
        "Выберите действие:\n"
        "\U0001f514 *Проверить сигналы* \u2014 сканирует топ-5 монет и отправит уведомления по качественным сделкам\n"
        "\U0001f3c6 *Лучшая сделка* \u2014 найдёт лучшую торговую возможность\n"
        "\U0001f4ca *Таймфреймы* \u2014 справка по удержанию сделки\n"
        "\U0001f441 *Отслеживания* \u2014 информация о трекинге SL/TP"
    )
    _send(chat_id, text, _main_menu_kb())


def _handle_timeframes_info(chat_id: int | str, callback_query_id: str) -> None:
    _answer_callback(callback_query_id)
    lines = ["*\u23f3 Время удержания по таймфрейму:*\n"]
    for tf, desc in HOLDING_PERIOD.items():
        lines.append(f"\u2022 *{tf}* \u2014 {desc}")
    lines.append("\n_Выбранный таймфрейм определяет рекомендуемое время удержания сделки._")
    _send(chat_id, "\n".join(lines), _main_menu_kb())


def _handle_signals_scan(chat_id: int | str, timeframe: str, callback_query_id: str) -> None:
    _answer_callback(callback_query_id, f"Сканирую {timeframe}...")
    _send(chat_id, f"\U0001f50d Сканирую топ-5 монет на *{timeframe}*...")

    items = _scan_coins(timeframe)

    notified: list[str] = []
    for item in items:
        d = item.model_dump()
        if is_high_quality_deal(d):
            msg = format_deal_message(d)
            _send(chat_id, msg)
            notified.append(item.coin)

    if notified:
        summary = f"\u2705 Отправлено {len(notified)} сигнал(ов): {', '.join(notified)}"
    else:
        best_conf = items[0].confidence if items else 0
        summary = (
            f"\U0001f6ab Сигналов не найдено (порог 75%).\n"
            f"Лучшая уверенность: *{best_conf}%*\n"
            f"Просканировано: {len(items)} монет"
        )
    _send(chat_id, summary, _main_menu_kb())


def _handle_best_deal(chat_id: int | str, timeframe: str, callback_query_id: str) -> None:
    _answer_callback(callback_query_id, f"Ищу лучшую сделку {timeframe}...")
    _send(chat_id, f"\U0001f50d Ищу лучшую сделку на *{timeframe}*...")

    items = _scan_coins(timeframe)

    if not items:
        _send(chat_id, "\u274c Не удалось просканировать монеты.", _main_menu_kb())
        return

    best = items[0]
    d = best.model_dump()
    msg = format_deal_message(d)
    _send(chat_id, msg, _main_menu_kb())


# --- Main dispatcher ----------------------------------------------------------

def handle_update(update: dict) -> None:
    """Process an incoming Telegram update (message or callback_query)."""
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            _handle_start(chat_id)
        elif text.startswith("/signals") or text.startswith("/check"):
            _send(chat_id, "Выберите таймфрейм для сканирования:", _timeframe_kb("sig"))
        elif text.startswith("/best"):
            _send(chat_id, "Выберите таймфрейм для лучшей сделки:", _timeframe_kb("best"))
        elif text.startswith("/watches"):
            _handle_watches_info(chat_id)
        elif text.startswith("/help"):
            _handle_start(chat_id)
        else:
            _handle_start(chat_id)

    elif "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")

        if data == "action_signals":
            _answer_callback(cb_id)
            _send(chat_id, "Выберите таймфрейм для сканирования:", _timeframe_kb("sig"))
        elif data == "action_best":
            _answer_callback(cb_id)
            _send(chat_id, "Выберите таймфрейм для лучшей сделки:", _timeframe_kb("best"))
        elif data == "action_timeframes_info":
            _handle_timeframes_info(chat_id, cb_id)
        elif data == "action_watches":
            _answer_callback(cb_id)
            _handle_watches_info(chat_id)
        elif data == "back_main":
            _answer_callback(cb_id)
            _handle_start(chat_id)
        elif data.startswith("sig_"):
            tf = data.removeprefix("sig_")
            if tf in TIMEFRAMES:
                _handle_signals_scan(chat_id, tf, cb_id)
        elif data.startswith("best_"):
            tf = data.removeprefix("best_")
            if tf in TIMEFRAMES:
                _handle_best_deal(chat_id, tf, cb_id)
        else:
            _answer_callback(cb_id, "Неизвестная команда")


def _handle_watches_info(chat_id: int | str) -> None:
    text = (
        "*\U0001f441 Отслеживание SL/TP*\n\n"
        "Как работает:\n"
        "1. Запустите анализ или лучшую сделку на сайте\n"
        "2. Нажмите кнопку *\U0001f4cc Отслеживать*\n"
        "3. Сайт будет проверять цену каждые 10 секунд\n"
        "4. При достижении SL или TP вам придёт уведомление в Telegram\n\n"
        "\u2757 _Важно: отслеживание работает пока открыта вкладка сайта в браузере._\n\n"
        "Сайт: [crypto-ai-eta.vercel.app](https://crypto-ai-eta.vercel.app)"
    )
    _send(chat_id, text, _main_menu_kb())


def set_webhook(base_url: str) -> dict | None:
    """Register the webhook URL with Telegram."""
    webhook_url = f"{base_url}/telegram-webhook"
    return _api("setWebhook", {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"],
    })

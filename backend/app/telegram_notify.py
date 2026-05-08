"""Telegram notification module for high-confidence deal alerts."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("crypto-ai.telegram")

TELEGRAM_API = "https://api.telegram.org"

HOLDING_PERIOD = {
    "15m": "скальп — до нескольких часов",
    "1h": "интрадей — до 1 дня",
    "4h": "свинг — 1–3 дня",
    "1d": "позиция — 1–7 дней",
    "1w": "долгосрочная — 1–4 недели",
}


def _get_config() -> tuple[str, str] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 10:
        return f"{v:.4f}"
    return f"{v:.6f}"


def _compute_rr(entry: float | None, stop: float | None, tp: float | None, direction: str) -> str:
    if entry is None or stop is None or tp is None or entry == stop:
        return "—"
    sign = 1 if direction == "long" else -1
    rr = (tp - entry) / abs(entry - stop) * sign
    return f"{rr:.2f}"


def format_deal_message(deal: dict) -> str:
    """Format a deal dict into a Telegram-friendly message."""
    direction = deal.get("direction", "flat")
    dir_emoji = {"long": "🟢 ЛОНГ", "short": "🔴 ШОРТ"}.get(direction, "⚪ ФЛЭТ")
    coin = deal.get("coin", "?")
    tf = deal.get("timeframe", "?")
    confidence = deal.get("confidence", 0)
    entry = deal.get("entry")
    stop_loss = deal.get("stop_loss")
    tp1 = deal.get("take_profit_1")
    tp2 = deal.get("take_profit_2")
    rr1 = _compute_rr(entry, stop_loss, tp1, direction)
    rr2 = _compute_rr(entry, stop_loss, tp2, direction)
    trend = deal.get("trend", "—")
    rsi = deal.get("rsi", 0)
    regime = deal.get("market_regime", "—")
    rationale = deal.get("rationale", "")
    price = deal.get("last_price")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    holding = HOLDING_PERIOD.get(tf, "—")

    lines = [
        f"🏆 *Сигнал: {coin}/USDT · {tf}*",
        f"",
        f"{dir_emoji} · Уверенность: *{confidence}%*",
        f"⏳ Удержание: {holding}",
        f"",
        f"📊 *Параметры сделки:*",
        f"  Цена: `{_fmt_price(price)}`",
        f"  Вход: `{_fmt_price(entry)}`",
        f"  Stop-loss: `{_fmt_price(stop_loss)}`",
        f"  TP1: `{_fmt_price(tp1)}` (RR {rr1})",
        f"  TP2: `{_fmt_price(tp2)}` (RR {rr2})",
        f"",
        f"📈 Тренд: {trend} · RSI: {rsi:.1f} · Режим: {regime}",
    ]
    if rationale:
        lines.append(f"")
        lines.append(f"💬 _{rationale}_")
    lines.append(f"")
    lines.append(f"⏰ {now}")
    lines.append(f"⚠️ _Не является финансовым советом_")

    return "\n".join(lines)


# --- Quality filter -----------------------------------------------------------

MIN_CONFIDENCE = 75
MIN_RR1 = 1.2
STRONG_TRENDS = {
    "bullish", "strong bullish", "bearish", "strong bearish",
    "восходящий", "нисходящий",
}


def is_high_quality_deal(deal: dict) -> bool:
    """Return True if the deal passes all quality filters for notification."""
    if deal.get("direction") == "flat":
        return False
    if (deal.get("confidence") or 0) < MIN_CONFIDENCE:
        return False

    entry = deal.get("entry")
    stop = deal.get("stop_loss")
    tp1 = deal.get("take_profit_1")
    direction = deal.get("direction", "flat")
    if entry is not None and stop is not None and tp1 is not None and entry != stop:
        sign = 1 if direction == "long" else -1
        rr = (tp1 - entry) / abs(entry - stop) * sign
        if rr < MIN_RR1:
            return False

    trend = (deal.get("trend") or "").lower()
    if trend and trend not in STRONG_TRENDS:
        if trend != "боковой":
            return False
        return False

    return True


def send_telegram_message(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    cfg = _get_config()
    if cfg is None:
        log.warning("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)")
        return False
    token, chat_id = cfg
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Telegram message sent successfully")
            return True
        log.warning("Telegram API returned %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to send Telegram message: %s", e)
        return False


def notify_if_worthy(deal: dict) -> bool:
    """Check deal quality and send Telegram notification if it passes filters."""
    if not is_high_quality_deal(deal):
        return False
    msg = format_deal_message(deal)
    return send_telegram_message(msg)

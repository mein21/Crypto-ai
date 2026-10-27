"""
bot_v2.py — Bybit Scalping Bot for PythonAnywhere (V5 API, pybit SDK)

Стратегия (15m по умолчанию):
  - EMA20 определяет направление.
  - Откат на 1 свечу + следующая свеча в сторону EMA = сигнал.
  - Объём последней закрытой свечи > 1.5× среднего за 20 баров — подтверждение.
  - SL = entry ± SL_ATR_MULT * ATR(14), TP = entry ± TP_ATR_MULT * ATR(14).
  - Все qty / TP / SL округлены к биржевому шагу (qtyStep / tickSize).

Защиты:
  - Дневной лимит убытков (реально считается из closed PnL Bybit).
  - Максимум сделок в день.
  - Спред-фильтр (по ticker bid/ask).
  - Сигнал смотрит только на ЗАКРЫТЫЕ свечи (closes[-2], не [-1]).
  - One-Way mode форсится на старте.

Запуск (PythonAnywhere):
  cron Tasks каждые 15 минут:
      cd /home/USER/bot_v2 && python3 bot_v2.py >> bot.log 2>&1

Конфиг — целиком из env (см. .env.example).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from pybit.unified_trading import HTTP


# ============================================================================
# Paths
# ============================================================================
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "bot_state.json"
LOG_FILE = BASE_DIR / "bot.log"


# ============================================================================
# Config (from env)
# ============================================================================
def _env(name: str, default: Optional[str] = None, cast=str):
    val = os.environ.get(name)
    if val is None or val == "":
        if default is None:
            raise SystemExit(f"Missing required env var: {name}")
        val = default
    try:
        return cast(val)
    except (TypeError, ValueError) as e:
        raise SystemExit(f"Bad value for {name}={val!r}: {e}")


def _envb(name: str, default: bool) -> bool:
    val = os.environ.get(name, "true" if default else "false")
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


CONFIG = {
    "api_key": _env("BYBIT_API_KEY"),
    "api_secret": _env("BYBIT_API_SECRET"),
    "telegram_token": _env("TG_TOKEN"),
    "telegram_chat_id": _env("TG_CHAT_ID"),
    "symbol": _env("BOT_SYMBOL", "BTCUSDT"),
    "category": _env("BOT_CATEGORY", "linear"),
    "interval": _env("BOT_INTERVAL", "15"),
    "leverage": _env("BOT_LEVERAGE", "5", int),
    "margin_usdt": _env("BOT_MARGIN_USDT", "10", float),
    "ema_period": _env("BOT_EMA_PERIOD", "20", int),
    "atr_period": _env("BOT_ATR_PERIOD", "14", int),
    "vol_period": _env("BOT_VOL_PERIOD", "20", int),
    "vol_mult": _env("BOT_VOL_MULT", "1.5", float),
    "tp_atr_mult": _env("BOT_TP_ATR_MULT", "1.5", float),
    "sl_atr_mult": _env("BOT_SL_ATR_MULT", "1.0", float),
    "max_daily_loss_usdt": _env("BOT_MAX_DAILY_LOSS_USDT", "3.0", float),
    "max_trades_per_day": _env("BOT_MAX_TRADES_PER_DAY", "5", int),
    "spread_max_pct": _env("BOT_SPREAD_MAX_PCT", "0.05", float),
    "tz": _env("BOT_TZ", "UTC"),
    "testnet": _envb("BOT_TESTNET", True),
    "dry_run": _envb("BOT_DRY_RUN", False),
}

LOCAL_TZ = ZoneInfo(CONFIG["tz"])


# ============================================================================
# Logging
# ============================================================================
def _setup_logging() -> logging.Logger:
    log = logging.getLogger("bot_v2")
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        return log
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


log = _setup_logging()


# ============================================================================
# Pure helpers (unit-tested in tests/test_bot_v2.py)
# ============================================================================
def round_step(value: float, step: float, mode: str = "down") -> float:
    """Round value to a multiple of step. mode: 'down' | 'up' | 'nearest'."""
    if step <= 0:
        return value
    n = value / step
    if mode == "down":
        return math.floor(n + 1e-12) * step
    if mode == "up":
        return math.ceil(n - 1e-12) * step
    return round(n) * step


def step_decimals(step: float) -> int:
    """Decimal places implied by step (e.g. 0.001 -> 3, 0.5 -> 1, 1 -> 0)."""
    if step >= 1:
        return 0
    s = f"{step:.10f}".rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0


def fmt_step(value: float, step: float, mode: str = "down") -> str:
    rounded = round_step(value, step, mode)
    decimals = step_decimals(step)
    return f"{rounded:.{decimals}f}"


def calc_ema(prices: list[float], period: int) -> Optional[float]:
    if len(prices) < period or period < 1:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p - ema) * multiplier + ema
    return ema


def calc_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
) -> Optional[float]:
    """Wilder's ATR."""
    n = len(closes)
    if n <= period or len(highs) != n or len(lows) != n:
        return None
    trs: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def detect_signal(
    closes: list[float],
    volumes: list[float],
    ema_period: int,
    vol_period: int,
    vol_mult: float,
) -> Optional[str]:
    """
    Сигнал по ПОСЛЕДНЕЙ ЗАКРЫТОЙ свече (closes[-2]).
    closes[-1] — текущая (потенциально незакрытая), её игнорируем.
    """
    need = max(ema_period + 3, vol_period + 3)
    if len(closes) < need or len(volumes) < need:
        return None
    last_close = closes[-2]
    prev_close = closes[-3]
    prev_prev_close = closes[-4]
    last_volume = volumes[-2]

    ema = calc_ema(closes[:-2], ema_period)
    if ema is None:
        return None

    avg_vol = sum(volumes[-(vol_period + 2) : -2]) / vol_period
    if avg_vol <= 0 or last_volume <= avg_vol * vol_mult:
        return None

    if (
        last_close > ema
        and prev_close < prev_prev_close
        and last_close > prev_close
    ):
        return "long"
    if (
        last_close < ema
        and prev_close > prev_prev_close
        and last_close < prev_close
    ):
        return "short"
    return None


# ============================================================================
# State
# ============================================================================
def _today_str() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def load_state() -> dict:
    default = {
        "daily_pnl_usdt": 0.0,  # signed: negative = loss
        "trades_today": 0,
        "last_date": _today_str(),
        "last_pnl_check_ms": _now_ms() - 24 * 3600 * 1000,  # look back 24h on first run
    }
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        log.error("State file corrupt (%s) — using defaults", e)
        send_telegram(f"WARNING: bot_state.json повреждён ({e}), сброс к defaults")
        return default


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


# ============================================================================
# Telegram
# ============================================================================
def send_telegram(message: str) -> None:
    token = CONFIG["telegram_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id:
        log.info("[TG-skip] %s", message)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=10)
            if r.status_code == 200:
                return
            log.warning("Telegram %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram error (try %d): %s", attempt + 1, e)
        time.sleep(2 ** attempt)
    log.error("Telegram failed after 3 retries")


# ============================================================================
# Bybit client
# ============================================================================
def _client() -> HTTP:
    return HTTP(
        testnet=CONFIG["testnet"],
        api_key=CONFIG["api_key"],
        api_secret=CONFIG["api_secret"],
        recv_window=5000,
        timeout=10,
    )


def _ok(resp: dict) -> bool:
    return bool(resp) and resp.get("retCode") == 0


def get_filters(client: HTTP, symbol: str) -> Tuple[float, float, float]:
    """Returns (qty_step, tick_size, min_order_qty)."""
    r = client.get_instruments_info(category=CONFIG["category"], symbol=symbol)
    if not _ok(r) or not r.get("result", {}).get("list"):
        raise RuntimeError(f"instruments-info failed: {r.get('retMsg')}")
    info = r["result"]["list"][0]
    qty_step = float(info["lotSizeFilter"]["qtyStep"])
    min_qty = float(info["lotSizeFilter"]["minOrderQty"])
    tick_size = float(info["priceFilter"]["tickSize"])
    return qty_step, tick_size, min_qty


def get_klines(client: HTTP, symbol: str, interval: str, limit: int = 100) -> list:
    r = client.get_kline(
        category=CONFIG["category"], symbol=symbol, interval=interval, limit=limit
    )
    if not _ok(r):
        raise RuntimeError(f"get_kline failed: {r.get('retMsg')}")
    candles = r["result"]["list"]
    # Bybit returns newest first → reverse.
    return list(reversed(candles))


def get_position_size(client: HTTP, symbol: str) -> float:
    r = client.get_positions(category=CONFIG["category"], symbol=symbol)
    if not _ok(r):
        raise RuntimeError(f"get_positions failed: {r.get('retMsg')}")
    total = 0.0
    for p in r["result"]["list"]:
        total += float(p.get("size", 0) or 0)
    return total


def get_ticker(client: HTTP, symbol: str) -> dict:
    r = client.get_tickers(category=CONFIG["category"], symbol=symbol)
    if not _ok(r) or not r["result"]["list"]:
        raise RuntimeError(f"tickers failed: {r.get('retMsg')}")
    return r["result"]["list"][0]


def ensure_one_way(client: HTTP, symbol: str) -> None:
    try:
        r = client.switch_position_mode(
            category=CONFIG["category"], symbol=symbol, mode=0
        )
        if not _ok(r) and r.get("retCode") != 110025:
            # 110025 = position mode not modified
            log.warning("switch_position_mode: %s", r.get("retMsg"))
    except Exception as e:  # noqa: BLE001
        log.warning("switch_position_mode raised: %s", e)


def ensure_leverage(client: HTTP, symbol: str, leverage: int) -> None:
    try:
        r = client.set_leverage(
            category=CONFIG["category"],
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        if not _ok(r) and r.get("retCode") != 110043:
            # 110043 = leverage not modified
            raise RuntimeError(f"set_leverage failed: {r.get('retMsg')}")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "110043" not in msg and "leverage not modified" not in msg.lower():
            raise


def fetch_closed_pnl_since(client: HTTP, symbol: str, since_ms: int) -> list[dict]:
    """Returns list of closed-pnl rows newer than since_ms."""
    rows: list[dict] = []
    cursor = ""
    for _ in range(5):  # safety: max 5 pages
        kwargs = dict(
            category=CONFIG["category"], symbol=symbol, limit=50,
            startTime=since_ms, endTime=_now_ms(),
        )
        if cursor:
            kwargs["cursor"] = cursor
        r = client.get_closed_pnl(**kwargs)
        if not _ok(r):
            log.warning("get_closed_pnl failed: %s", r.get("retMsg"))
            break
        page = r["result"].get("list", []) or []
        rows.extend(page)
        cursor = r["result"].get("nextPageCursor") or ""
        if not cursor or len(page) < 50:
            break
    return rows


# ============================================================================
# Trading core
# ============================================================================
def reset_if_new_day(state: dict) -> None:
    today = _today_str()
    if state.get("last_date") != today:
        log.info("New day — resetting daily counters (was %s, now %s)", state.get("last_date"), today)
        state["daily_pnl_usdt"] = 0.0
        state["trades_today"] = 0
        state["last_date"] = today


def update_daily_pnl(client: HTTP, state: dict) -> None:
    """Pull closed PnL since last check, accumulate daily_pnl_usdt."""
    since_ms = int(state.get("last_pnl_check_ms") or 0)
    rows = fetch_closed_pnl_since(client, CONFIG["symbol"], since_ms)
    pnl = 0.0
    last_seen = since_ms
    for row in rows:
        try:
            ts = int(row.get("createdTime") or row.get("updatedTime") or 0)
        except (TypeError, ValueError):
            ts = 0
        try:
            p = float(row.get("closedPnl") or 0)
        except (TypeError, ValueError):
            p = 0.0
        pnl += p
        last_seen = max(last_seen, ts)
    if pnl != 0:
        log.info("Closed PnL since last check: %.4f USDT (%d trades)", pnl, len(rows))
    state["daily_pnl_usdt"] = float(state.get("daily_pnl_usdt") or 0) + pnl
    state["last_pnl_check_ms"] = max(last_seen, _now_ms())


def parse_klines(candles: list) -> tuple[list[float], list[float], list[float], list[float]]:
    """Returns (highs, lows, closes, volumes)."""
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return highs, lows, closes, volumes


def compute_levels(
    direction: str, entry: float, atr: float, tick: float
) -> tuple[float, float]:
    """Compute TP/SL prices, rounded to tick.

    Rounding convention:
      - SL is rounded AWAY from entry (wider stop, more room for noise).
      - TP is rounded TOWARD entry (closer fill, scalper-friendly).
    """
    sl_dist = CONFIG["sl_atr_mult"] * atr
    tp_dist = CONFIG["tp_atr_mult"] * atr
    if direction == "long":
        sl_raw = entry - sl_dist
        tp_raw = entry + tp_dist
        sl = round_step(sl_raw, tick, "down")  # further below entry
        tp = round_step(tp_raw, tick, "down")  # closer to entry
    else:  # short
        sl_raw = entry + sl_dist
        tp_raw = entry - tp_dist
        sl = round_step(sl_raw, tick, "up")    # further above entry
        tp = round_step(tp_raw, tick, "up")    # closer to entry
    return tp, sl


def compute_qty(margin_usdt: float, leverage: int, price: float,
                qty_step: float, min_qty: float) -> float:
    """notional = margin × leverage; qty = notional / price, rounded down to qty_step."""
    notional = margin_usdt * leverage
    raw = notional / price
    qty = round_step(raw, qty_step, "down")
    if qty < min_qty:
        return 0.0
    return qty


def place_market_order(
    client: HTTP, symbol: str, side: str, qty: float, qty_step: float,
    tp: float, sl: float, tick: float,
) -> dict:
    qty_str = fmt_step(qty, qty_step, "down")
    tp_str = fmt_step(tp, tick, "down" if side == "Sell" else "up")
    sl_str = fmt_step(sl, tick, "up" if side == "Sell" else "down")
    log.info(
        "Placing order: %s %s qty=%s tp=%s sl=%s",
        side, symbol, qty_str, tp_str, sl_str,
    )
    if CONFIG["dry_run"]:
        log.info("[dry-run] order skipped")
        return {"retCode": 0, "result": {"orderId": "dry-run"}}
    r = client.place_order(
        category=CONFIG["category"],
        symbol=symbol,
        side=side,
        orderType="Market",
        qty=qty_str,
        takeProfit=tp_str,
        stopLoss=sl_str,
        tpTriggerBy="MarkPrice",
        slTriggerBy="MarkPrice",
        timeInForce="IOC",
        positionIdx=0,
    )
    return r


# ============================================================================
# Main run
# ============================================================================
def run_bot() -> int:
    log.info("=" * 60)
    log.info("Bybit Scalping Bot v2 started (testnet=%s, dry_run=%s)",
             CONFIG["testnet"], CONFIG["dry_run"])
    log.info("Symbol=%s interval=%s margin=%.2f leverage=%dx tz=%s",
             CONFIG["symbol"], CONFIG["interval"],
             CONFIG["margin_usdt"], CONFIG["leverage"], CONFIG["tz"])

    state = load_state()
    try:
        client = _client()

        reset_if_new_day(state)
        update_daily_pnl(client, state)

        # Daily guards.
        if -state["daily_pnl_usdt"] >= CONFIG["max_daily_loss_usdt"]:
            msg = (f"Дневной лимит убытков достигнут: "
                   f"PnL={state['daily_pnl_usdt']:.2f} USDT ≤ "
                   f"-{CONFIG['max_daily_loss_usdt']}. Стоп до завтра.")
            log.warning(msg)
            send_telegram(f"⛔ {msg}")
            return 0
        if state["trades_today"] >= CONFIG["max_trades_per_day"]:
            log.info("Лимит сделок: %d/%d. Стоп.",
                     state["trades_today"], CONFIG["max_trades_per_day"])
            return 0

        # If position already open — do nothing.
        size = get_position_size(client, CONFIG["symbol"])
        if size > 0:
            log.info("Уже есть открытая позиция size=%s — пропускаем.", size)
            return 0

        # Filters & mode.
        qty_step, tick_size, min_qty = get_filters(client, CONFIG["symbol"])
        log.info("Filters: qtyStep=%s tickSize=%s minQty=%s",
                 qty_step, tick_size, min_qty)
        ensure_one_way(client, CONFIG["symbol"])
        ensure_leverage(client, CONFIG["symbol"], CONFIG["leverage"])

        # Klines + signal.
        candles = get_klines(client, CONFIG["symbol"], CONFIG["interval"], limit=100)
        if len(candles) < 50:
            log.info("Мало свечей (%d) — ждём.", len(candles))
            return 0
        highs, lows, closes, volumes = parse_klines(candles)
        atr = calc_atr(highs, lows, closes[:-1], CONFIG["atr_period"])
        signal = detect_signal(
            closes, volumes,
            CONFIG["ema_period"], CONFIG["vol_period"], CONFIG["vol_mult"],
        )
        if signal is None or atr is None:
            log.info("Нет сигнала (atr=%s, signal=%s).", atr, signal)
            return 0

        # Spread filter.
        ticker = get_ticker(client, CONFIG["symbol"])
        bid = float(ticker.get("bid1Price") or 0)
        ask = float(ticker.get("ask1Price") or 0)
        last = float(ticker.get("lastPrice") or 0)
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
            log.info("Spread %.4f%% (limit %.4f%%)", spread_pct, CONFIG["spread_max_pct"])
            if spread_pct > CONFIG["spread_max_pct"]:
                log.info("Spread слишком большой — пропуск.")
                return 0

        entry = last or closes[-2]
        tp, sl = compute_levels(signal, entry, atr, tick_size)
        qty = compute_qty(
            CONFIG["margin_usdt"], CONFIG["leverage"], entry, qty_step, min_qty,
        )
        if qty <= 0:
            msg = (f"Размер позиции меньше minQty={min_qty}. "
                   f"Увеличь BOT_MARGIN_USDT или BOT_LEVERAGE.")
            log.warning(msg)
            send_telegram(f"⚠️ {msg}")
            return 0

        log.info(
            "SIGNAL %s | entry≈%.6f | TP=%.6f | SL=%.6f | ATR=%.6f | qty=%s",
            signal.upper(), entry, tp, sl, atr, fmt_step(qty, qty_step),
        )

        side = "Buy" if signal == "long" else "Sell"
        result = place_market_order(
            client, CONFIG["symbol"], side, qty, qty_step, tp, sl, tick_size,
        )
        if not _ok(result):
            err = result.get("retMsg", "unknown") if result else "no response"
            log.error("place_order failed: %s", err)
            send_telegram(f"❌ Ошибка ордера: {err}")
            return 1

        # Verify fill (market+IOC may partial).
        time.sleep(1.5)
        try:
            actual = get_position_size(client, CONFIG["symbol"])
            log.info("Position size after order: %s (requested %s)", actual, qty)
        except Exception as e:  # noqa: BLE001
            log.warning("position re-check failed: %s", e)
            actual = qty

        state["trades_today"] += 1
        rr = (CONFIG["tp_atr_mult"] / CONFIG["sl_atr_mult"])
        msg = (
            f"<b>📈 Открыта сделка</b>\n"
            f"Пара: {CONFIG['symbol']}\n"
            f"Сторона: <b>{'ЛОНГ' if signal == 'long' else 'ШОРТ'}</b>\n"
            f"Вход: <b>{entry:.6f}</b>\n"
            f"TP: <b>{tp:.6f}</b>\n"
            f"SL: <b>{sl:.6f}</b>\n"
            f"RR: <b>{rr:.2f}</b>\n"
            f"Маржа: ${CONFIG['margin_usdt']:.2f} × {CONFIG['leverage']}x = "
            f"${CONFIG['margin_usdt'] * CONFIG['leverage']:.2f} нотионал\n"
            f"Сделок сегодня: {state['trades_today']}/{CONFIG['max_trades_per_day']}\n"
            f"Дневной PnL: {state['daily_pnl_usdt']:+.2f} USDT"
        )
        send_telegram(msg)
        return 0
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Unhandled error: %s", e)
        try:
            send_telegram(f"❌ Bot crash: {e}")
        except Exception:  # noqa: BLE001
            pass
        return 2
    finally:
        try:
            save_state(state)
        except Exception as e:  # noqa: BLE001
            log.error("Failed to save state: %s", e)


if __name__ == "__main__":
    sys.exit(run_bot())

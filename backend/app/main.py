"""FastAPI app exposing the /analyze endpoint and serving the static frontend."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .alignment import compute_alignment
from .analytics import fetch_analytics, analytics_summary_for_prompt
from .backtest import walk_forward_backtest
from .chart import render_chart_b64
from .context import (
    fetch_correlation_vs_btc,
    fetch_fear_greed,
    fetch_higher_tf_trends,
    fetch_news_for_coin,
)
from .data import SYMBOL_MAP, SYMBOL_MAP_USDT, fetch_ohlcv, _exchange, EXCHANGE_ORDER
from .indicators import compute_all
from .llm import analyze, analyze_fast
from .news_enrich import enrich_news
from .onchain import fetch_onchain
from .order_flow import compute_order_flow
from .patterns import detect_patterns
from .telegram_bot import handle_update, set_webhook
from .telegram_notify import notify_if_worthy
from .schemas import (
    Alignment,
    AnalyzeRequest,
    AnalyzeResponse,
    BestDealItem,
    BestDealRequest,
    BestDealResponse,
    BtcOnchain,
    CandlePattern,
    ContextResponse,
    CorrelationVsBtc,
    EthOnchain,
    FearGreed,
    HtfTrend,
    NewsItem,
    Onchain,
    OrderFlow,
    Sentiment,
    StrategyStats,
    VolumeProfile,
)
from .sentiment import compute_sentiment
from .volume_profile import compute_volume_profile

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("crypto-ai")

app = FastAPI(title="Crypto AI Analyzer", version="0.1.0")

origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "llm": {
            "groq": bool(os.getenv("GROQ_API_KEY", "").strip()),
            "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        },
    }


@app.get("/coins")
def coins() -> dict:
    return {
        "coins": list(SYMBOL_MAP.keys()),
        "timeframes": ["15m", "1h", "4h", "1d", "1w"],
    }


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze_endpoint(req: AnalyzeRequest) -> AnalyzeResponse:
    try:
        df = fetch_ohlcv(req.coin, req.timeframe)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Не удалось получить данные с биржи: {e}") from e

    if len(df) < 60:
        raise HTTPException(status_code=502, detail="Недостаточно свечных данных для анализа.")

    ind = compute_all(df)
    summary = ind.summary()

    # Auxiliary context — none of these should fail the request.
    htf_raw = fetch_higher_tf_trends(req.coin, req.timeframe)
    fng_raw = fetch_fear_greed()
    news_raw = enrich_news(req.coin, fetch_news_for_coin(req.coin, limit=5))
    onchain_raw = fetch_onchain(req.coin)

    try:
        atr_last = float(ind.atr.iloc[-1])
    except Exception:  # noqa: BLE001
        atr_last = 0.0
    patterns_raw = detect_patterns(
        df,
        lookback=10,
        support=ind.support,
        resistance=ind.resistance,
        atr=atr_last,
        trend_label=summary.get("trend"),
    )

    # New strategic modules — all computed from already-fetched OHLCV / context.
    vp_raw = compute_volume_profile(df)
    flow_bundle = compute_order_flow(df)
    flow_raw = flow_bundle.to_summary() if flow_bundle is not None else None
    alignment_raw = compute_alignment(summary, htf_raw)

    daily_close: pd.Series | None = None
    if req.timeframe == "1d":
        daily_close = df["close"].astype(float)
    else:
        try:
            daily_df = fetch_ohlcv(req.coin, "1d")
            daily_close = daily_df["close"].astype(float)
        except Exception as e:  # noqa: BLE001
            log.info("Daily close fetch failed for sentiment: %s", e)
    sentiment_raw = compute_sentiment(
        fear_greed=fng_raw, news=news_raw, daily_close=daily_close
    )

    try:
        strategy_raw = walk_forward_backtest(df)
    except Exception as e:  # noqa: BLE001
        log.warning("Walk-forward backtest failed: %s", e)
        strategy_raw = None

    # Analytics center: long/short ratio, funding rate, OI, top traders
    try:
        analytics_raw = fetch_analytics(req.coin)
    except Exception as e:  # noqa: BLE001
        log.warning("Analytics fetch failed: %s", e)
        analytics_raw = {}

    extra_context = {
        "htf_trends": htf_raw,
        "fear_greed": fng_raw,
        "news_titles": [n.get("title_ru") or n.get("title", "") for n in news_raw][:5],
        "onchain": onchain_raw,
        "coin": req.coin,
        "patterns": patterns_raw,
        "volume_profile": vp_raw,
        "order_flow": flow_raw,
        "alignment": alignment_raw,
        "sentiment": sentiment_raw,
        "strategy_stats": strategy_raw,
        "analytics": analytics_raw,
    }
    analysis = analyze(req.coin, req.timeframe, summary, extra_context=extra_context)
    analysis.patterns = [CandlePattern(**p) for p in patterns_raw]
    chart_b64 = render_chart_b64(
        req.coin,
        req.timeframe,
        ind,
        signal=analysis.signal,
        patterns=patterns_raw,
        volume_profile=vp_raw,
        order_flow=flow_bundle,
    )

    onchain_obj: Onchain | None = None
    if onchain_raw and req.coin == "BTC":
        onchain_obj = Onchain(btc=BtcOnchain(**onchain_raw))
    elif onchain_raw and req.coin == "ETH":
        onchain_obj = Onchain(eth=EthOnchain(**onchain_raw))

    return AnalyzeResponse(
        analysis=analysis,
        chart_png_b64=chart_b64,
        indicators=summary,
        last_price=float(summary["close"]),
        htf_trends=[HtfTrend(**h) for h in htf_raw],
        news=[NewsItem(**n) for n in news_raw],
        fear_greed=FearGreed(**fng_raw) if fng_raw else None,
        onchain=onchain_obj,
        volume_profile=VolumeProfile(**vp_raw) if vp_raw else None,
        order_flow=OrderFlow(**flow_raw) if flow_raw else None,
        alignment=Alignment(**alignment_raw) if alignment_raw else None,
        sentiment=Sentiment(**sentiment_raw) if sentiment_raw else None,
        strategy_stats=StrategyStats(**strategy_raw) if strategy_raw else None,
    )


@app.post("/best-deal", response_model=BestDealResponse)
def best_deal_endpoint(req: BestDealRequest) -> BestDealResponse:
    """Scan all coins on the given timeframe and return the best trading opportunity."""
    items: list[BestDealItem] = []
    for coin in SYMBOL_MAP:
        try:
            df = fetch_ohlcv(coin, req.timeframe)
            if len(df) < 60:
                continue
            ind = compute_all(df)
            summary = ind.summary()
            result = analyze(coin, req.timeframe, summary)
            sig = result.signal
            items.append(
                BestDealItem(
                    coin=coin,
                    timeframe=req.timeframe,
                    direction=sig.direction,
                    confidence=sig.confidence,
                    entry=sig.entry,
                    entry_type=sig.entry_type,
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
        except Exception as e:  # noqa: BLE001
            log.warning("Best-deal scan failed for %s: %s", coin, e)
            continue

    items.sort(key=lambda x: (x.direction != "flat", x.confidence), reverse=True)
    best = items[0] if items else None

    full_analysis: AnalyzeResponse | None = None
    if best:
        try:
            coin = best.coin
            df = fetch_ohlcv(coin, req.timeframe)
            ind = compute_all(df)
            summary = ind.summary()
            htf_raw = fetch_higher_tf_trends(coin, req.timeframe)
            fng_raw = fetch_fear_greed()
            news_raw = enrich_news(coin, fetch_news_for_coin(coin, limit=5))
            onchain_raw = fetch_onchain(coin)
            try:
                atr_last = float(ind.atr.iloc[-1])
            except Exception:  # noqa: BLE001
                atr_last = 0.0
            patterns_raw = detect_patterns(
                df, lookback=10, support=ind.support, resistance=ind.resistance,
                atr=atr_last, trend_label=summary.get("trend"),
            )
            # Strategy modules — match /analyze so all 5 cards populate.
            vp_raw = compute_volume_profile(df)
            flow_bundle = compute_order_flow(df)
            flow_raw = flow_bundle.to_summary() if flow_bundle is not None else None
            alignment_raw = compute_alignment(summary, htf_raw)
            daily_close: pd.Series | None = None
            if req.timeframe == "1d":
                daily_close = df["close"].astype(float)
            else:
                try:
                    daily_df = fetch_ohlcv(coin, "1d")
                    daily_close = daily_df["close"].astype(float)
                except Exception as e:  # noqa: BLE001
                    log.info("Daily close fetch failed for sentiment (best-deal): %s", e)
            sentiment_raw = compute_sentiment(
                fear_greed=fng_raw, news=news_raw, daily_close=daily_close
            )
            try:
                strategy_raw = walk_forward_backtest(df)
            except Exception as e:  # noqa: BLE001
                log.warning("Walk-forward backtest failed (best-deal): %s", e)
                strategy_raw = None
            try:
                analytics_raw = fetch_analytics(coin)
            except Exception as e:  # noqa: BLE001
                log.warning("Analytics fetch failed (best-deal): %s", e)
                analytics_raw = {}
            extra_context = {
                "htf_trends": htf_raw, "fear_greed": fng_raw,
                "news_titles": [n.get("title_ru") or n.get("title", "") for n in news_raw][:5],
                "onchain": onchain_raw, "coin": coin, "patterns": patterns_raw,
                "volume_profile": vp_raw, "order_flow": flow_raw,
                "alignment": alignment_raw, "sentiment": sentiment_raw,
                "strategy_stats": strategy_raw, "analytics": analytics_raw,
            }
            analysis = analyze(coin, req.timeframe, summary, extra_context=extra_context)
            analysis.patterns = [CandlePattern(**p) for p in patterns_raw]
            chart_b64 = render_chart_b64(
                coin, req.timeframe, ind, signal=analysis.signal, patterns=patterns_raw,
                volume_profile=vp_raw, order_flow=flow_bundle,
            )
            onchain_obj: Onchain | None = None
            if onchain_raw and coin == "BTC":
                onchain_obj = Onchain(btc=BtcOnchain(**onchain_raw))
            elif onchain_raw and coin == "ETH":
                onchain_obj = Onchain(eth=EthOnchain(**onchain_raw))
            full_analysis = AnalyzeResponse(
                analysis=analysis, chart_png_b64=chart_b64, indicators=summary,
                last_price=float(summary["close"]),
                htf_trends=[HtfTrend(**h) for h in htf_raw],
                news=[NewsItem(**n) for n in news_raw],
                fear_greed=FearGreed(**fng_raw) if fng_raw else None,
                onchain=onchain_obj,
                volume_profile=VolumeProfile(**vp_raw) if vp_raw else None,
                order_flow=OrderFlow(**flow_raw) if flow_raw else None,
                alignment=Alignment(**alignment_raw) if alignment_raw else None,
                sentiment=Sentiment(**sentiment_raw) if sentiment_raw else None,
                strategy_stats=StrategyStats(**strategy_raw) if strategy_raw else None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Full analysis for best deal %s failed: %s", best.coin, e)

    if best:
        notify_if_worthy(best.model_dump())

    return BestDealResponse(best=best, scanned=len(items), all_deals=items[:5], full_analysis=full_analysis)


# Top coins for fast cron scan (fit Vercel Hobby 10s timeout)
CRON_COINS = ["BTC", "ETH", "SOL", "BNB", "XRP"]
CRON_TIMEFRAMES = ["4h", "1d"]


@app.api_route("/notify-check", methods=["GET", "POST"])
def notify_check_endpoint(req: BestDealRequest | None = None) -> dict:
    """Scan coins and send Telegram notifications for high-quality deals.

    GET  (cron): fast rules-based scan of top 5 coins on 4h+1d timeframes.
    POST (manual): full LLM analysis of all 10 coins on the selected timeframe.
    """
    is_cron = req is None
    coins = CRON_COINS if is_cron else list(SYMBOL_MAP.keys())
    timeframes = CRON_TIMEFRAMES if is_cron else [req.timeframe]
    analyzer = analyze_fast if is_cron else analyze

    items: list[BestDealItem] = []
    for tf in timeframes:
        for coin in coins:
            try:
                df = fetch_ohlcv(coin, tf)
                if len(df) < 60:
                    continue
                ind = compute_all(df)
                summary = ind.summary()
                result = analyzer(coin, tf, summary)
                sig = result.signal
                items.append(
                    BestDealItem(
                        coin=coin, timeframe=tf,
                        direction=sig.direction, confidence=sig.confidence,
                        entry=sig.entry, stop_loss=sig.stop_loss,
                        take_profit_1=sig.take_profit_1, take_profit_2=sig.take_profit_2,
                        rationale=sig.rationale, last_price=float(summary["close"]),
                        trend=result.trend, rsi=round(float(summary["rsi"]), 1),
                        market_regime=result.market_regime,
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Notify-check scan failed for %s/%s: %s", coin, tf, e)
                continue

    items.sort(key=lambda x: (x.direction != "flat", x.confidence), reverse=True)
    notified = []
    for item in items:
        d = item.model_dump()
        if notify_if_worthy(d):
            notified.append(f"{item.coin}/{item.timeframe}")

    return {
        "scanned": len(items),
        "notified": notified,
        "best_confidence": items[0].confidence if items else 0,
    }


@app.post("/telegram-webhook")
def telegram_webhook(update: dict) -> dict:
    """Handle incoming Telegram bot updates (messages and callback queries)."""
    try:
        handle_update(update)
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram webhook error: %s", e)
    return {"ok": True}


@app.get("/set-telegram-webhook")
def set_tg_webhook() -> dict:
    """Register the Telegram webhook URL. Call once after deployment."""
    base = os.getenv("VERCEL_URL", "").strip()
    if not base:
        base = os.getenv("BASE_URL", "https://crypto-ai-eta.vercel.app").strip()
    if not base.startswith("http"):
        base = f"https://{base}"
    result = set_webhook(base)
    return {"ok": True, "result": result, "base_url": base}


@app.get("/price/{coin}")
def price_endpoint(coin: str) -> dict:
    """Return the current price for a coin via exchange ticker."""
    coin = coin.upper()
    if coin not in SYMBOL_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported coin: {coin}")
    for ex_name, symbol_map in EXCHANGE_ORDER:
        if coin not in symbol_map:
            continue
        symbol = symbol_map[coin]
        try:
            ex = _exchange(ex_name)
            ticker = ex.fetch_ticker(symbol)
            return {
                "coin": coin,
                "price": ticker["last"],
                "exchange": ex_name,
            }
        except Exception:  # noqa: BLE001
            continue
    raise HTTPException(status_code=502, detail="Cannot fetch price from any exchange")


@app.post("/send-alert")
def send_alert_endpoint(payload: dict) -> dict:
    """Send a custom SL/TP hit alert to Telegram."""
    from .telegram_notify import send_telegram_message, HOLDING_PERIOD, _fmt_price

    coin = payload.get("coin", "?")
    tf = payload.get("timeframe", "?")
    direction = payload.get("direction", "flat")
    hit_type = payload.get("hit_type", "?")  # "SL", "TP1", "TP2"
    hit_price = payload.get("hit_price")
    entry = payload.get("entry")
    stop_loss = payload.get("stop_loss")
    tp1 = payload.get("take_profit_1")
    tp2 = payload.get("take_profit_2")

    dir_emoji = {"long": "\U0001f7e2", "short": "\U0001f534"}.get(direction, "\u26aa")
    dir_text = {"long": "\u041b\u041e\u041d\u0413", "short": "\u0428\u041e\u0420\u0422"}.get(direction, "\u2014")

    if hit_type == "SL":
        icon = "\U0001f6a8"
        result_text = "Stop-loss \u0441\u0440\u0430\u0431\u043e\u0442\u0430\u043b"
    elif hit_type == "TP1":
        icon = "\U0001f3af"
        result_text = "Take-profit 1 \u0434\u043e\u0441\u0442\u0438\u0433\u043d\u0443\u0442"
    elif hit_type == "TP2":
        icon = "\U0001f3af\U0001f3af"
        result_text = "Take-profit 2 \u0434\u043e\u0441\u0442\u0438\u0433\u043d\u0443\u0442"
    else:
        icon = "\u2757"
        result_text = hit_type

    lines = [
        f"{icon} *{result_text}: {coin}/USDT \u00b7 {tf}*",
        f"",
        f"{dir_emoji} {dir_text}",
        f"\U0001f4b0 \u0426\u0435\u043d\u0430 \u0441\u0440\u0430\u0431\u0430\u0442\u044b\u0432\u0430\u043d\u0438\u044f: `{_fmt_price(hit_price)}`",
        f"",
        f"\U0001f4ca *\u041f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u044b \u0441\u0434\u0435\u043b\u043a\u0438:*",
        f"  \u0412\u0445\u043e\u0434: `{_fmt_price(entry)}`",
        f"  Stop-loss: `{_fmt_price(stop_loss)}`",
        f"  TP1: `{_fmt_price(tp1)}`",
        f"  TP2: `{_fmt_price(tp2)}`",
    ]

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"")
    lines.append(f"\u23f0 {now}")

    msg = "\n".join(lines)
    ok = send_telegram_message(msg)
    return {"ok": ok}


# --- Server-side watches storage (/tmp file for persistence across warm invocations) ---
import json as _json

_WATCHES_FILE = Path("/tmp/watches.json")


def _save_watches(watches: list[dict]) -> None:
    try:
        _WATCHES_FILE.write_text(_json.dumps(watches, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _load_watches() -> list[dict]:
    try:
        if _WATCHES_FILE.exists():
            return _json.loads(_WATCHES_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return []


@app.post("/watches/sync")
def watches_sync(payload: dict) -> dict:
    """Receive the current watches list from the frontend."""
    watches = payload.get("watches", [])
    _save_watches(watches)
    return {"ok": True, "count": len(watches)}


@app.get("/watches")
def watches_get() -> dict:
    """Return current active watches (for Telegram bot)."""
    return {"watches": _load_watches()}


def analyze_watches(watches: list[dict] | None = None) -> dict:
    """Analyze watches: fetch price, run indicators, calculate TP/SL probabilities.

    Can be called directly (from Telegram bot) or via the HTTP endpoint.
    """
    if watches is None:
        watches = _load_watches()
    results = []
    for w in watches:
        coin = w.get("coin", "BTC")
        tf = w.get("timeframe", "4h")
        direction = w.get("direction", "flat")
        entry = w.get("entry")
        stop_loss = w.get("stop_loss")
        tp1 = w.get("take_profit_1")
        tp2 = w.get("take_profit_2")
        tp1_hit = w.get("tp1_hit", False)

        # Fetch current price
        current_price = None
        for ex_name, symbol_map in EXCHANGE_ORDER:
            if coin not in symbol_map:
                continue
            try:
                ex = _exchange(ex_name)
                ticker = ex.fetch_ticker(symbol_map[coin])
                current_price = ticker["last"]
                break
            except Exception:  # noqa: BLE001
                continue

        if current_price is None or entry is None or stop_loss is None:
            results.append({**w, "error": "no_price"})
            continue

        # Run indicator analysis
        try:
            df = fetch_ohlcv(coin, tf)
            if len(df) < 60:
                results.append({**w, "current_price": current_price, "error": "insufficient_data"})
                continue
            ind = compute_all(df)
            summary = ind.summary()
        except Exception:  # noqa: BLE001
            results.append({**w, "current_price": current_price, "error": "analysis_failed"})
            continue

        # --- Strategy-based TP/SL probability ---
        is_long = direction == "long"
        trend = summary.get("trend", "боковой")
        rsi_val = summary.get("rsi", 50)
        macd_hist = summary.get("macd_hist", 0)
        macd_state = summary.get("macd_state", "нейтрально")
        bb_lower = summary.get("bb_lower", 0)
        bb_upper = summary.get("bb_upper", 0)
        atr_val = summary.get("atr", 0)
        ema_fast = summary.get("ema_fast", 0)
        ema_slow = summary.get("ema_slow", 0)
        support_levels = summary.get("support", [])
        resistance_levels = summary.get("resistance", [])

        # Base probability from distance ratio
        dist_to_sl = abs(current_price - stop_loss)
        dist_to_tp = abs(current_price - (tp1 if tp1 else entry))
        total_dist = dist_to_sl + dist_to_tp
        if total_dist > 0:
            tp_base = (dist_to_sl / total_dist) * 100
        else:
            tp_base = 50.0

        tp_prob = tp_base
        adjustments = []

        # 1) Trend alignment
        trend_supports = (is_long and trend == "восходящий") or (not is_long and trend == "нисходящий")
        trend_against = (is_long and trend == "нисходящий") or (not is_long and trend == "восходящий")
        if trend_supports:
            tp_prob += 12
            adjustments.append("Тренд подтверждает направление (+12%)")
        elif trend_against:
            tp_prob -= 15
            adjustments.append("Тренд против позиции (-15%)")
        else:
            adjustments.append("Боковой тренд (0%)")

        # 2) RSI
        if is_long:
            if rsi_val >= 75:
                tp_prob -= 10
                adjustments.append(f"RSI {rsi_val:.0f} — перекупленность (-10%)")
            elif rsi_val >= 60:
                tp_prob += 5
                adjustments.append(f"RSI {rsi_val:.0f} — бычий импульс (+5%)")
            elif rsi_val <= 30:
                tp_prob += 8
                adjustments.append(f"RSI {rsi_val:.0f} — зона отскока (+8%)")
        else:
            if rsi_val <= 25:
                tp_prob -= 10
                adjustments.append(f"RSI {rsi_val:.0f} — перепроданность (-10%)")
            elif rsi_val <= 40:
                tp_prob += 5
                adjustments.append(f"RSI {rsi_val:.0f} — медвежий импульс (+5%)")
            elif rsi_val >= 70:
                tp_prob += 8
                adjustments.append(f"RSI {rsi_val:.0f} — зона разворота (+8%)")

        # 3) MACD
        macd_confirms = (is_long and macd_state == "бычий") or (not is_long and macd_state == "медвежий")
        macd_against = (is_long and macd_state == "медвежий") or (not is_long and macd_state == "бычий")
        if macd_confirms:
            tp_prob += 8
            adjustments.append("MACD подтверждает (+8%)")
        elif macd_against:
            tp_prob -= 8
            adjustments.append("MACD против (-8%)")

        # 4) EMA alignment
        if is_long and ema_fast > ema_slow:
            tp_prob += 5
            adjustments.append("EMA20 > EMA50 (+5%)")
        elif not is_long and ema_fast < ema_slow:
            tp_prob += 5
            adjustments.append("EMA20 < EMA50 (+5%)")
        elif is_long and ema_fast < ema_slow:
            tp_prob -= 5
            adjustments.append("EMA20 < EMA50 (-5%)")
        elif not is_long and ema_fast > ema_slow:
            tp_prob -= 5
            adjustments.append("EMA20 > EMA50 (-5%)")

        # 5) Bollinger position
        if bb_upper > bb_lower:
            bb_pos = (current_price - bb_lower) / (bb_upper - bb_lower)
            if is_long and bb_pos > 0.9:
                tp_prob -= 7
                adjustments.append("Цена у верхней Боллинджера (-7%)")
            elif not is_long and bb_pos < 0.1:
                tp_prob -= 7
                adjustments.append("Цена у нижней Боллинджера (-7%)")

        # 6) Support/resistance obstacles
        if is_long and tp1 and resistance_levels:
            obstacles = [r for r in resistance_levels if current_price < r < tp1]
            if obstacles:
                tp_prob -= min(len(obstacles) * 3, 9)
                adjustments.append(f"{len(obstacles)} сопротивлени(е/я) до TP (-{min(len(obstacles)*3,9)}%)")
        elif not is_long and tp1 and support_levels:
            obstacles = [s for s in support_levels if tp1 < s < current_price]
            if obstacles:
                tp_prob -= min(len(obstacles) * 3, 9)
                adjustments.append(f"{len(obstacles)} поддерж(ка/ки) до TP (-{min(len(obstacles)*3,9)}%)")

        tp_prob = max(5, min(95, tp_prob))
        sl_prob = max(5, min(95, 100 - tp_prob))

        # --- P&L ---
        if is_long:
            pnl_pct = ((current_price - entry) / entry) * 100
        else:
            pnl_pct = ((entry - current_price) / entry) * 100

        # --- Recommendations ---
        recommendations = []
        if pnl_pct > 0 and tp1 and not tp1_hit:
            progress = abs(current_price - entry) / abs(tp1 - entry) * 100 if tp1 != entry else 0
            if progress >= 70:
                recommendations.append("Цена прошла >70% до TP1 — рассмотрите трейлинг-стоп")
            elif progress >= 40:
                recommendations.append("Цена прошла >40% до TP1 — передвиньте стоп в безубыток")

        if tp1_hit:
            recommendations.append("TP1 достигнут — перенесите стоп к TP1, ждите TP2")

        if trend_against:
            recommendations.append("Тренд развернулся — рассмотрите ранний выход")

        if (is_long and rsi_val >= 75) or (not is_long and rsi_val <= 25):
            recommendations.append("RSI в экстремальной зоне — возможен откат")

        if sl_prob >= 60:
            recommendations.append("Высокий риск SL — рассмотрите уменьшение позиции")

        if not recommendations:
            if tp_prob >= 65:
                recommendations.append("Позиция выглядит хорошо — держите по плану")
            else:
                recommendations.append("Следите за индикаторами — нет явного сигнала")

        results.append({
            "coin": coin,
            "timeframe": tf,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "tp1_hit": tp1_hit,
            "current_price": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "tp_probability": round(tp_prob, 1),
            "sl_probability": round(sl_prob, 1),
            "adjustments": adjustments,
            "recommendations": recommendations,
            "indicators": {
                "trend": trend,
                "rsi": round(rsi_val, 1),
                "macd_state": macd_state,
                "ema_fast": round(ema_fast, 2),
                "ema_slow": round(ema_slow, 2),
            },
        })

    return {"watches": results}


@app.post("/watches/analyze")
def watches_analyze_endpoint() -> dict:
    """HTTP endpoint wrapper for analyze_watches."""
    return analyze_watches()


@app.post("/watches/analyze-and-send")
def watches_analyze_and_send(payload: dict) -> dict:
    """Receive watches from the frontend, analyze, and send results to Telegram.

    This avoids the cross-instance problem on Vercel serverless:
    the frontend (localStorage) sends watches directly, so no server-side
    persistence is needed.
    """
    from .telegram_notify import send_telegram_message, _fmt_price

    watches = payload.get("watches", [])
    if not watches:
        return {"ok": False, "error": "no_watches"}

    _save_watches(watches)

    data = analyze_watches(watches)
    analyzed = data.get("watches", [])

    sent_count = 0
    for w in analyzed:
        if w.get("error"):
            continue

        coin = w.get("coin", "?")
        tf = w.get("timeframe", "?")
        direction = w.get("direction", "flat")
        dir_emoji = {"long": "\U0001f7e2 ЛОНГ", "short": "\U0001f534 ШОРТ"}.get(direction, "\u26aa ФЛЭТ")
        entry = w.get("entry")
        stop_loss = w.get("stop_loss")
        tp1 = w.get("take_profit_1")
        tp2 = w.get("take_profit_2")
        tp1_hit = w.get("tp1_hit", False)
        current_price = w.get("current_price")
        pnl_pct = w.get("pnl_pct", 0)
        tp_prob = w.get("tp_probability", 50)
        sl_prob = w.get("sl_probability", 50)
        adjustments = w.get("adjustments", [])
        recommendations = w.get("recommendations", [])
        indicators = w.get("indicators", {})

        pnl_emoji = "\U0001f4b0" if pnl_pct >= 0 else "\U0001f4c9"
        pnl_sign = "+" if pnl_pct >= 0 else ""

        tp_bar = "\U0001f7e2" * max(1, round(tp_prob / 10)) + "\u26aa" * max(0, 10 - max(1, round(tp_prob / 10)))
        sl_bar = "\U0001f534" * max(1, round(sl_prob / 10)) + "\u26aa" * max(0, 10 - max(1, round(sl_prob / 10)))

        lines = [
            f"*\U0001f441 {coin}/USDT \u00b7 {tf}*",
            f"{dir_emoji}",
            f"",
            f"\U0001f4b5 Цена: `{_fmt_price(current_price)}`",
            f"\U0001f3af Вход: `{_fmt_price(entry)}`",
            f"{pnl_emoji} P&L: `{pnl_sign}{pnl_pct:.2f}%`",
            f"",
            f"*Шансы:*",
            f"TP: {tp_bar} *{tp_prob:.0f}%*",
            f"SL: {sl_bar} *{sl_prob:.0f}%*",
            f"",
            f"*\U0001f4ca Уровни:*",
            f"  SL: `{_fmt_price(stop_loss)}`",
            f"  TP1: `{_fmt_price(tp1)}`{'  \u2705' if tp1_hit else ''}",
        ]
        if tp2:
            lines.append(f"  TP2: `{_fmt_price(tp2)}`")

        lines.append(f"")
        lines.append(f"*\U0001f4c8 Индикаторы:*")
        lines.append(f"  Тренд: {indicators.get('trend', '\u2014')}")
        lines.append(f"  RSI: {indicators.get('rsi', '\u2014')}")
        lines.append(f"  MACD: {indicators.get('macd_state', '\u2014')}")

        if adjustments:
            lines.append(f"")
            lines.append(f"*\U0001f9ee Анализ:*")
            for adj in adjustments[:5]:
                lines.append(f"  \u2022 {adj}")

        if recommendations:
            lines.append(f"")
            lines.append(f"*\U0001f4a1 Рекомендации:*")
            for rec in recommendations:
                lines.append(f"  \u27a1 {rec}")

        if send_telegram_message("\n".join(lines)):
            sent_count += 1

    if sent_count > 0:
        send_telegram_message(f"\U0001f441 Активных отслеживаний: *{sent_count}*")

    return {"ok": True, "sent": sent_count, "analyzed": len(analyzed)}


@app.get("/context", response_model=ContextResponse)
def context_endpoint() -> ContextResponse:
    fng = fetch_fear_greed()
    corr = fetch_correlation_vs_btc(window_days=30)
    return ContextResponse(
        fear_greed=FearGreed(**fng) if fng else None,
        correlation=CorrelationVsBtc(**corr) if corr else None,
    )


# --- Static frontend (same-origin, avoids CORS entirely) -------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FRONTEND_DIR = next(
    (_REPO_ROOT / d for d in ("public", "frontend") if (_REPO_ROOT / d).exists()),
    None,
)
if _FRONTEND_DIR is not None:
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_FRONTEND_DIR / "index.html"))

    @app.get("/styles.css")
    def styles() -> FileResponse:
        return FileResponse(str(_FRONTEND_DIR / "styles.css"))

    @app.get("/app.js")
    def app_js() -> FileResponse:
        return FileResponse(str(_FRONTEND_DIR / "app.js"))

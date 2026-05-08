"""FastAPI app exposing the /analyze endpoint and serving the static frontend."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
from .patterns import detect_patterns
from .telegram_bot import handle_update, set_webhook
from .telegram_notify import notify_if_worthy
from .schemas import (
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
)

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

    extra_context = {
        "htf_trends": htf_raw,
        "fear_greed": fng_raw,
        "news_titles": [n.get("title_ru") or n.get("title", "") for n in news_raw][:5],
        "onchain": onchain_raw,
        "coin": req.coin,
        "patterns": patterns_raw,
    }
    analysis = analyze(req.coin, req.timeframe, summary, extra_context=extra_context)
    analysis.patterns = [CandlePattern(**p) for p in patterns_raw]
    chart_b64 = render_chart_b64(
        req.coin,
        req.timeframe,
        ind,
        signal=analysis.signal,
        patterns=patterns_raw,
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
            extra_context = {
                "htf_trends": htf_raw, "fear_greed": fng_raw,
                "news_titles": [n.get("title_ru") or n.get("title", "") for n in news_raw][:5],
                "onchain": onchain_raw, "coin": coin, "patterns": patterns_raw,
            }
            analysis = analyze(coin, req.timeframe, summary, extra_context=extra_context)
            analysis.patterns = [CandlePattern(**p) for p in patterns_raw]
            chart_b64 = render_chart_b64(coin, req.timeframe, ind, signal=analysis.signal, patterns=patterns_raw)
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

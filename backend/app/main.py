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
from .backtest import walk_forward_backtest
from .chart import render_chart_b64
from .context import (
    fetch_correlation_matrix,
    fetch_fear_greed,
    fetch_higher_tf_trends,
    fetch_news_for_coin,
)
from .data import SYMBOL_MAP, fetch_ohlcv
from .indicators import compute_all
from .llm import analyze
from .order_flow import compute_order_flow
from .patterns import detect_patterns
from .schemas import (
    Alignment,
    AnalyzeRequest,
    AnalyzeResponse,
    CandlePattern,
    ContextResponse,
    CorrelationMatrix,
    FearGreed,
    HtfTrend,
    NewsItem,
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
    news_raw = fetch_news_for_coin(req.coin, limit=5)

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

    extra_context = {
        "htf_trends": htf_raw,
        "fear_greed": fng_raw,
        "news_titles": [n.get("title", "") for n in news_raw][:5],
        "patterns": patterns_raw,
        "volume_profile": vp_raw,
        "order_flow": flow_raw,
        "alignment": alignment_raw,
        "sentiment": sentiment_raw,
        "strategy_stats": strategy_raw,
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

    return AnalyzeResponse(
        analysis=analysis,
        chart_png_b64=chart_b64,
        indicators=summary,
        last_price=float(summary["close"]),
        htf_trends=[HtfTrend(**h) for h in htf_raw],
        news=[NewsItem(**n) for n in news_raw],
        fear_greed=FearGreed(**fng_raw) if fng_raw else None,
        volume_profile=VolumeProfile(**vp_raw) if vp_raw else None,
        order_flow=OrderFlow(**flow_raw) if flow_raw else None,
        alignment=Alignment(**alignment_raw) if alignment_raw else None,
        sentiment=Sentiment(**sentiment_raw) if sentiment_raw else None,
        strategy_stats=StrategyStats(**strategy_raw) if strategy_raw else None,
    )


@app.get("/context", response_model=ContextResponse)
def context_endpoint() -> ContextResponse:
    fng = fetch_fear_greed()
    corr = fetch_correlation_matrix(window_days=30)
    return ContextResponse(
        fear_greed=FearGreed(**fng) if fng else None,
        correlation=CorrelationMatrix(**corr) if corr else None,
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

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
    fetch_correlation_matrix,
    fetch_fear_greed,
    fetch_higher_tf_trends,
    fetch_news_for_coin,
)
from .data import SYMBOL_MAP, fetch_ohlcv
from .indicators import compute_all
from .llm import analyze
from .news_enrich import enrich_news
from .onchain import fetch_onchain
from .schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    BtcOnchain,
    ContextResponse,
    CorrelationMatrix,
    EthOnchain,
    FearGreed,
    HtfTrend,
    NewsItem,
    Onchain,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("crypto-ai")

from .autotrade.config import AutoTradeConfig  # noqa: E402
from .autotrade.crypto_keys import KeyVault  # noqa: E402
from .autotrade.routes import _bind as autotrade_router  # noqa: E402
from .autotrade import scheduler as autotrade_scheduler  # noqa: E402
from .autotrade import store as autotrade_store  # noqa: E402

autotrade_cfg = AutoTradeConfig.from_env()

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if autotrade_cfg.enabled:
        try:
            autotrade_store.init_db(autotrade_cfg.db_path)
            vault = KeyVault(autotrade_cfg.master_key)
            autotrade_scheduler.start(autotrade_cfg, vault)
            log.info("autotrade enabled (db=%s)", autotrade_cfg.db_path)
        except Exception as e:  # noqa: BLE001
            log.error("autotrade boot failed: %s", e)
    yield
    if autotrade_cfg.enabled:
        await autotrade_scheduler.shutdown()


app = FastAPI(title="Crypto AI Analyzer", version="0.1.0", lifespan=_lifespan)

origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if autotrade_cfg.enabled:
    app.include_router(autotrade_router(autotrade_cfg))
    log.info("autotrade routes mounted")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "llm": {
            "groq": bool(os.getenv("GROQ_API_KEY", "").strip()),
            "gemini": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        },
        "autotrade": {
            "enabled": autotrade_cfg.enabled,
            "scheduler_running": autotrade_scheduler.get_scheduler() is not None,
        },
    }


@app.get("/config")
def public_config() -> dict:
    """Public client config — exposed to frontend so it knows where the worker lives.

    On Vercel the worker URL is configured via the WORKER_URL env var (set in
    Vercel project settings). Worker-side it returns its own origin so that
    embed-tests work even without a Vercel.
    """
    return {
        "worker_url": os.getenv("WORKER_URL", "").strip() or None,
        "autotrade_local": autotrade_cfg.enabled,
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

    extra_context = {
        "htf_trends": htf_raw,
        "fear_greed": fng_raw,
        "news_titles": [n.get("title_ru") or n.get("title", "") for n in news_raw][:5],
        "onchain": onchain_raw,
        "coin": req.coin,
    }
    analysis = analyze(req.coin, req.timeframe, summary, extra_context=extra_context)
    chart_b64 = render_chart_b64(req.coin, req.timeframe, ind, signal=analysis.signal)

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

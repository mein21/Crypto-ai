# crypto-ai backend

FastAPI service that:

1. Fetches OHLCV candles from Binance via `ccxt` (no API key needed).
2. Computes technical indicators (EMA 20/50/200, RSI, MACD, Bollinger, ATR).
3. Detects swing-pivot support/resistance levels.
4. Calls Google Gemini (`GEMINI_API_KEY`) for a structured analysis (JSON), with
   a deterministic rules-based fallback if the LLM is unavailable.
5. Renders an annotated dark-mode candlestick chart (`mplfinance`) with
   support/resistance, EMAs, Bollinger, RSI, MACD, and entry/SL/TP markers.

## Run locally

```bash
uv venv && source .venv/bin/activate
uv pip install -e .
cp .env.example .env  # paste your GEMINI_API_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000/docs for the OpenAPI playground.

## Endpoints

- `GET /healthz` — health + LLM availability flag
- `GET /coins` — supported coins and timeframes
- `POST /analyze` — body: `{"coin": "BTC", "timeframe": "1h"}` → returns analysis + base64 PNG

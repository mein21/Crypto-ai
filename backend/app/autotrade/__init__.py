"""Auto-trading worker package.

This package is mounted on the FastAPI app only when the environment variable
`AUTOTRADE_ENABLED=1`. On Vercel (where Bybit is geo-blocked and serverless
limits prohibit a long-running scheduler) it stays dormant; on the Fly.io
worker it boots a scheduler that scans markets, picks the highest-confidence
trade idea and forwards it to Bybit via pybit V5.
"""
from __future__ import annotations

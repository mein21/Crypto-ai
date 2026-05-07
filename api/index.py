"""Vercel serverless entrypoint.

Vercel's @vercel/python builder picks up `app` (an ASGI/WSGI app) from this
module and serves it. We re-export the FastAPI instance defined in
backend/app/main.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# matplotlib needs a writable cache dir on serverless
os.environ.setdefault("MPLCONFIGDIR", "/tmp/.matplotlib")
Path("/tmp/.matplotlib").mkdir(parents=True, exist_ok=True)

# Make `app.*` importable (the backend lives in /backend/app)
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.main import app  # noqa: E402,F401  (re-exported for Vercel)

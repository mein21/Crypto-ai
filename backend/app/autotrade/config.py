"""Runtime configuration for the autotrade worker.

All knobs are environment-variable driven so behaviour can be tuned on
Fly.io without redeploying. Defaults are deliberately conservative — the
goal is "smallest plausible loss", not "biggest plausible win".
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class AutoTradeConfig:
    enabled: bool
    db_path: str
    master_key: str
    coins: tuple[str, ...]
    timeframes: tuple[str, ...]
    scan_interval_sec: int
    min_confidence: int
    min_rr: float
    max_concurrent: int
    default_leverage: float
    default_position_pct: float
    daily_loss_pct: float
    weekly_loss_pct: float
    breakeven_at_r: float
    cors_origins: tuple[str, ...]
    public_base_url: str

    @classmethod
    def from_env(cls) -> "AutoTradeConfig":
        coins = tuple(
            c.strip().upper()
            for c in os.getenv(
                "AT_COINS", "BTC,ETH,BNB,SOL,XRP,ADA,DOGE,AVAX,TON,DOT"
            ).split(",")
            if c.strip()
        )
        tfs = tuple(
            t.strip()
            for t in os.getenv("AT_TIMEFRAMES", "1h,4h").split(",")
            if t.strip()
        )
        cors = tuple(
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "*").split(",")
            if o.strip()
        )
        return cls(
            enabled=_b("AUTOTRADE_ENABLED", False),
            db_path=os.getenv("AT_DB_PATH", "/data/autotrade.db"),
            master_key=os.getenv("AUTOTRADE_MASTER_KEY", "").strip(),
            coins=coins,
            timeframes=tfs,
            scan_interval_sec=_i("AT_SCAN_INTERVAL_SEC", 300),
            min_confidence=_i("AT_MIN_CONFIDENCE", 60),
            min_rr=_f("AT_MIN_RR", 1.3),
            max_concurrent=_i("AT_MAX_CONCURRENT", 1),
            default_leverage=_f("AT_LEVERAGE", 3.0),
            default_position_pct=_f("AT_POSITION_PCT", 2.0),
            daily_loss_pct=_f("AT_DAILY_LOSS_PCT", 5.0),
            weekly_loss_pct=_f("AT_WEEKLY_LOSS_PCT", 12.0),
            breakeven_at_r=_f("AT_BREAKEVEN_AT_R", 0.7),
            cors_origins=cors,
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip(),
        )

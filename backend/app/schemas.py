from typing import Literal, Optional
from pydantic import BaseModel, Field

Coin = Literal["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "TON", "DOT"]
Timeframe = Literal["15m", "1h", "4h", "1d", "1w"]


class AnalyzeRequest(BaseModel):
    coin: Coin
    timeframe: Timeframe


class Level(BaseModel):
    price: float
    label: str = ""


class Signal(BaseModel):
    direction: Literal["long", "short", "flat"] = "flat"
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    confidence: int = Field(0, ge=0, le=100)
    rationale: str = ""


class Analysis(BaseModel):
    coin: str
    timeframe: str
    market_regime: str = ""
    trend: str = ""
    key_levels: dict[str, list[float]] = Field(default_factory=lambda: {"support": [], "resistance": []})
    indicators_summary: dict[str, str] = Field(default_factory=dict)
    signal: Signal = Field(default_factory=Signal)
    narrative: str = ""
    risks: list[str] = Field(default_factory=list)
    disclaimer: str = "Это не финансовый совет. Крипторынок крайне волатилен."


class AnalyzeResponse(BaseModel):
    analysis: Analysis
    chart_png_b64: str
    indicators: dict
    last_price: float

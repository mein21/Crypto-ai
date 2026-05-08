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


class HtfTrend(BaseModel):
    tf: str
    trend: str
    rsi: float
    rsi_state: str
    macd_state: str
    change_pct_30bars: float
    close: float


class NewsItem(BaseModel):
    title: str
    url: str
    source: str = ""
    ts: int = 0
    image: str = ""
    title_ru: Optional[str] = None
    impact: Optional[Literal["high", "medium", "low"]] = None
    sentiment: Optional[Literal["bullish", "neutral", "bearish"]] = None


class FearGreed(BaseModel):
    value: int
    classification: str
    timestamp: Optional[str] = None


class CorrelationMatrix(BaseModel):
    labels: list[str]
    matrix: list[list[float]]
    window_days: int
    n_observations: int


class BtcFees(BaseModel):
    fastest: int = 0
    half_hour: int = 0
    hour: int = 0
    economy: int = 0
    minimum: int = 0


class BtcOnchain(BaseModel):
    fees_sat_per_vb: BtcFees
    mempool_count: int
    mempool_vsize_mb: float
    mempool_total_fee_btc: float
    block_height: int
    hashrate_eh: Optional[float] = None
    difficulty_progress_pct: float
    difficulty_change_pct: float
    blocks_to_retarget: int


class EthGas(BaseModel):
    slow: float
    standard: float
    fast: float


class EthOnchain(BaseModel):
    gas_gwei: EthGas
    base_fee_gwei: float
    current_gas_gwei: float
    block_number: int
    congestion_pct: Optional[float] = None


class Onchain(BaseModel):
    btc: Optional[BtcOnchain] = None
    eth: Optional[EthOnchain] = None


class AnalyzeResponse(BaseModel):
    analysis: Analysis
    chart_png_b64: str
    indicators: dict
    last_price: float
    htf_trends: list[HtfTrend] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    fear_greed: Optional[FearGreed] = None
    onchain: Optional[Onchain] = None


class ContextResponse(BaseModel):
    fear_greed: Optional[FearGreed] = None
    correlation: Optional[CorrelationMatrix] = None

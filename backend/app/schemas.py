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


class CandlePattern(BaseModel):
    name: str
    name_ru: str
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    kind: Literal["reversal", "continuation", "indecision"] = "indecision"
    strength: int = Field(1, ge=1, le=3)
    base_strength: int = Field(1, ge=1, le=3)
    ts: int = 0
    bar_index: int = 0
    context: str = ""


class Analysis(BaseModel):
    coin: str
    timeframe: str
    market_regime: str = ""
    trend: str = ""
    key_levels: dict[str, list[float]] = Field(default_factory=lambda: {"support": [], "resistance": []})
    indicators_summary: dict[str, str] = Field(default_factory=dict)
    patterns: list[CandlePattern] = Field(default_factory=list)
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


class FearGreed(BaseModel):
    value: int
    classification: str
    timestamp: Optional[str] = None


class CorrelationMatrix(BaseModel):
    labels: list[str]
    matrix: list[list[float]]
    window_days: int
    n_observations: int


class VolumeProfileBin(BaseModel):
    price: float
    volume: float
    share: float


class VolumeProfile(BaseModel):
    poc: float
    vah: float
    val: float
    bin_size: float
    n_bins: int
    lookback_bars: int
    position: Literal["above_va", "inside_va", "below_va"]
    distance_to_poc_pct: float
    hvn: list[float] = Field(default_factory=list)
    lvn: list[float] = Field(default_factory=list)
    bins: list[VolumeProfileBin] = Field(default_factory=list)


class OrderFlow(BaseModel):
    cvd_value: float
    cvd_slope: float
    buy_pressure_pct: float
    divergence: Literal["bullish", "bearish", "none"] = "none"
    divergence_note: str = ""


class AlignmentBlock(BaseModel):
    tf: str
    weight: float
    score: float
    trend: str = ""
    macd_state: str = ""
    rsi: Optional[float] = None


class Alignment(BaseModel):
    score: float = 0.0  # 0..100 absolute strength
    direction: int = 0  # -1 / 0 / +1
    label: str = ""
    breakdown: list[AlignmentBlock] = Field(default_factory=list)


class SentimentComponents(BaseModel):
    fear_greed: float = 50.0
    news: float = 50.0
    news_breakdown: dict = Field(default_factory=dict)
    momentum: float = 50.0
    momentum_pct: float = 0.0


class Sentiment(BaseModel):
    score: float = 50.0
    label: str = ""
    components: SentimentComponents = Field(default_factory=SentimentComponents)


class StrategyWindowStats(BaseModel):
    window: int
    bars: int
    total_trades: int = 0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    profit_factor: float = 0.0
    expectancy_atr: float = 0.0
    longs: int = 0
    shorts: int = 0


class StrategyStats(BaseModel):
    total_trades: int = 0
    win_rate: float = 0.0
    avg_rr: float = 0.0
    profit_factor: float = 0.0
    expectancy_atr: float = 0.0
    longs: int = 0
    shorts: int = 0
    lookback_bars: int = 0
    windows: list[StrategyWindowStats] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    analysis: Analysis
    chart_png_b64: str
    indicators: dict
    last_price: float
    htf_trends: list[HtfTrend] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    fear_greed: Optional[FearGreed] = None
    volume_profile: Optional[VolumeProfile] = None
    order_flow: Optional[OrderFlow] = None
    alignment: Optional[Alignment] = None
    sentiment: Optional[Sentiment] = None
    strategy_stats: Optional[StrategyStats] = None


class ContextResponse(BaseModel):
    fear_greed: Optional[FearGreed] = None
    correlation: Optional[CorrelationMatrix] = None

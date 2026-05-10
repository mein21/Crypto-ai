"""Technical indicators (computed manually so we don't depend on fragile TA libs).

Implements: EMA, RSI, MACD, Bollinger Bands, ATR, plus simple support/resistance
detection based on swing pivots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple
import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Disambiguate the divide-by-zero / NaN cases. When loss == 0 and gain > 0
    # RSI is 100 by definition; when both are 0 (totally flat) treat it as 50.
    flat = (avg_gain == 0) & (avg_loss == 0)
    only_gain = (avg_loss == 0) & (avg_gain > 0)
    out = out.where(~only_gain, 100.0)
    out = out.where(~flat, 50.0)
    return out.fillna(50)


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average Directional Index — Wilder smoothing.

    Used as a trend-strength gate (≥25 = strong trend, <20 = chop). Prevents
    the rules-based engine from issuing trend-following signals during
    ranging conditions where it has no edge.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)
    alpha = 1 / length
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def bollinger(series: pd.Series, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    mid = sma(series, length)
    std = series.rolling(window=length, min_periods=length).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return pd.DataFrame({"bb_lower": lower, "bb_mid": mid, "bb_upper": upper})


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def find_pivots(df: pd.DataFrame, left: int = 5, right: int = 5) -> Tuple[list[float], list[float]]:
    """Detect simple swing highs and lows.

    A point is a swing high if it is the highest in `left` bars before and `right` bars after.
    Symmetric definition for swing low. Returns (resistance_prices, support_prices) sorted desc/asc.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    resistance: list[float] = []
    support: list[float] = []
    for i in range(left, n - right):
        window_high = highs[i - left : i + right + 1]
        window_low = lows[i - left : i + right + 1]
        if highs[i] == window_high.max():
            resistance.append(float(highs[i]))
        if lows[i] == window_low.min():
            support.append(float(lows[i]))
    return resistance, support


def cluster_levels(prices: list[float], current: float, atr_value: float, max_levels: int = 4) -> list[float]:
    """Cluster nearby levels (within ~0.5 * ATR) and pick the most relevant ones near current price."""
    if not prices:
        return []
    tol = max(atr_value * 0.5, current * 0.002)
    sorted_p = sorted(prices)
    clusters: list[list[float]] = []
    for p in sorted_p:
        if clusters and abs(p - clusters[-1][-1]) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    means = [float(np.mean(c)) for c in clusters]
    # rank by proximity to current price
    means.sort(key=lambda x: abs(x - current))
    return means[:max_levels]


def _classify_macd(macd_df: pd.DataFrame) -> str:
    """Six-bucket MACD state — captures both side and momentum direction.

    States: бычий-разгон / бычий / разворот вверх / медвежий-разгон / медвежий
            / разворот вниз / нейтрально
    """
    if len(macd_df) < 3:
        return "нейтрально"
    macd_v = float(macd_df["macd"].iloc[-1])
    sig_v = float(macd_df["signal"].iloc[-1])
    hist = macd_df["hist"]
    h0 = float(hist.iloc[-1])
    h1 = float(hist.iloc[-2])
    above = macd_v > sig_v
    expanding = abs(h0) > abs(h1)
    flipped_up = h0 > 0 and h1 <= 0
    flipped_down = h0 < 0 and h1 >= 0
    if flipped_up:
        return "разворот вверх"
    if flipped_down:
        return "разворот вниз"
    if above and h0 > 0:
        return "бычий-разгон" if expanding else "бычий"
    if not above and h0 < 0:
        return "медвежий-разгон" if expanding else "медвежий"
    return "нейтрально"


def _bb_width_state(bb_df: pd.DataFrame, lookback: int = 100) -> tuple[float, str]:
    """Bollinger band width as fraction of mid + percentile-based regime label."""
    if bb_df.empty or len(bb_df) < 20:
        return 0.0, "нет данных"
    width = (bb_df["bb_upper"] - bb_df["bb_lower"]).abs() / bb_df["bb_mid"].replace(0, np.nan)
    width = width.dropna()
    if width.empty:
        return 0.0, "нет данных"
    last = float(width.iloc[-1])
    tail = width.tail(lookback)
    if len(tail) < 20 or tail.max() == 0:
        return last, "нет данных"
    rank = (tail < last).mean()
    if rank <= 0.2:
        regime = "сжатие"  # squeeze — breakout-prone
    elif rank >= 0.8:
        regime = "расширение"  # expansion — late move, expect contraction
    else:
        regime = "нормальная"
    return last, regime


@dataclass
class IndicatorBundle:
    df: pd.DataFrame
    ema_fast: pd.Series = field(default=None)  # type: ignore[assignment]
    ema_slow: pd.Series = field(default=None)  # type: ignore[assignment]
    ema_long: pd.Series = field(default=None)  # type: ignore[assignment]
    rsi: pd.Series = field(default=None)  # type: ignore[assignment]
    macd: pd.DataFrame = field(default=None)  # type: ignore[assignment]
    bb: pd.DataFrame = field(default=None)  # type: ignore[assignment]
    atr: pd.Series = field(default=None)  # type: ignore[assignment]
    adx: pd.Series = field(default=None)  # type: ignore[assignment]
    support: list[float] = field(default_factory=list)
    resistance: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        last = self.df.iloc[-1]
        ema_fast_v = float(self.ema_fast.iloc[-1])
        ema_slow_v = float(self.ema_slow.iloc[-1])
        ema_long_v = float(self.ema_long.iloc[-1])
        rsi_v = float(self.rsi.iloc[-1])
        macd_v = float(self.macd["macd"].iloc[-1])
        sig_v = float(self.macd["signal"].iloc[-1])
        hist_v = float(self.macd["hist"].iloc[-1])
        bb_l = float(self.bb["bb_lower"].iloc[-1])
        bb_m = float(self.bb["bb_mid"].iloc[-1]) if not np.isnan(self.bb["bb_mid"].iloc[-1]) else float("nan")
        bb_u = float(self.bb["bb_upper"].iloc[-1])
        atr_v = float(self.atr.iloc[-1])
        adx_v = float(self.adx.iloc[-1]) if self.adx is not None and not np.isnan(self.adx.iloc[-1]) else 0.0
        close = float(last["close"])

        if close > ema_long_v and ema_fast_v > ema_slow_v:
            trend = "восходящий"
        elif close < ema_long_v and ema_fast_v < ema_slow_v:
            trend = "нисходящий"
        else:
            trend = "боковой"

        if rsi_v >= 70:
            rsi_state = "перекупленность"
        elif rsi_v <= 30:
            rsi_state = "перепроданность"
        else:
            rsi_state = "нейтрально"

        macd_state = _classify_macd(self.macd)
        bb_width, bb_state = _bb_width_state(self.bb)

        if adx_v >= 25:
            adx_state = "сильный"
        elif adx_v <= 18:
            adx_state = "слабый"
        else:
            adx_state = "умеренный"

        # ATR as percentage of price — used by callers as a volatility filter.
        atr_pct = (atr_v / close * 100.0) if close > 0 else 0.0

        vol = self.df["volume"].astype(float)
        vol_sma20 = vol.rolling(20).mean()
        vol_last = float(vol.iloc[-1])
        vol_avg = float(vol_sma20.iloc[-1]) if len(vol_sma20.dropna()) > 0 else vol_last
        vol_ratio = vol_last / vol_avg if vol_avg > 0 else 1.0

        return {
            "close": close,
            "ema_fast": ema_fast_v,
            "volume_ratio": round(vol_ratio, 2),
            "ema_slow": ema_slow_v,
            "ema_long": ema_long_v,
            "rsi": rsi_v,
            "rsi_state": rsi_state,
            "macd": macd_v,
            "macd_signal": sig_v,
            "macd_hist": hist_v,
            "macd_state": macd_state,
            "bb_lower": bb_l,
            "bb_mid": bb_m,
            "bb_upper": bb_u,
            "bb_width": bb_width,
            "bb_state": bb_state,
            "atr": atr_v,
            "atr_pct": atr_pct,
            "adx": adx_v,
            "adx_state": adx_state,
            "trend": trend,
            "support": self.support,
            "resistance": self.resistance,
        }


def compute_all(df: pd.DataFrame) -> IndicatorBundle:
    close = df["close"]
    ema_fast = ema(close, 20)
    ema_slow = ema(close, 50)
    ema_long = ema(close, 200)
    rsi_s = rsi(close, 14)
    macd_df = macd(close)
    bb_df = bollinger(close)
    atr_s = atr(df, 14)
    adx_s = adx(df, 14)
    res_raw, sup_raw = find_pivots(df, left=5, right=5)
    last_close = float(close.iloc[-1])
    atr_v = float(atr_s.iloc[-1]) if not np.isnan(atr_s.iloc[-1]) else last_close * 0.01
    resistance = [p for p in cluster_levels(res_raw, last_close, atr_v, max_levels=6) if p >= last_close]
    support = [p for p in cluster_levels(sup_raw, last_close, atr_v, max_levels=6) if p <= last_close]
    # ensure we have at least 2 of each
    if len(resistance) < 2:
        resistance = sorted(set(resistance + cluster_levels(res_raw, last_close, atr_v, max_levels=6)))[:4]
    if len(support) < 2:
        support = sorted(set(support + cluster_levels(sup_raw, last_close, atr_v, max_levels=6)), reverse=True)[:4]
    return IndicatorBundle(
        df=df,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        ema_long=ema_long,
        rsi=rsi_s,
        macd=macd_df,
        bb=bb_df,
        atr=atr_s,
        adx=adx_s,
        support=sorted(set(support))[:4],
        resistance=sorted(set(resistance))[:4],
    )

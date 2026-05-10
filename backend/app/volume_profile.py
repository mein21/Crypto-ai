"""Volume profile (POC / VAH / VAL / HVN / LVN).

Classical TPO-style volume profile built from OHLCV candles. Volume of each
candle is distributed across price bins proportional to the share of the
candle's high–low range that falls inside each bin.

This is a deterministic, dependency-free implementation; no external L2/depth
data is required (which we don't have anyway).

Returned dict shape::

    {
        "poc": float,                  # price of bin with max volume
        "vah": float,                  # value-area high (top of 70% volume zone)
        "val": float,                  # value-area low
        "bin_size": float,
        "n_bins": int,
        "lookback_bars": int,
        "position": "above_va" | "inside_va" | "below_va",
        "distance_to_poc_pct": float,  # signed: + means price > POC
        "hvn": [float, float, ...],    # top-3 high-volume nodes (excl. POC)
        "lvn": [float, float, ...],    # top-3 low-volume nodes inside [val, vah]
        "bins": [
            {"price": float, "volume": float, "share": float}, ...
        ],
    }
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class _Bin:
    low: float
    high: float
    volume: float = 0.0

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


def _build_bins(price_low: float, price_high: float, n_bins: int) -> list[_Bin]:
    edges = np.linspace(price_low, price_high, n_bins + 1)
    return [_Bin(low=float(edges[i]), high=float(edges[i + 1])) for i in range(n_bins)]


def _distribute_candle(bins: list[_Bin], candle_low: float, candle_high: float, candle_vol: float) -> None:
    """Distribute a single candle's volume across bins proportional to overlap."""
    rng = max(candle_high - candle_low, 1e-12)
    for b in bins:
        overlap = min(b.high, candle_high) - max(b.low, candle_low)
        if overlap > 0:
            b.volume += candle_vol * (overlap / rng)


def compute_volume_profile(
    df: pd.DataFrame,
    *,
    lookback: int = 200,
    n_bins: int = 24,
    value_area_pct: float = 0.70,
) -> dict | None:
    """Compute a TPO-style volume profile from the last `lookback` candles."""
    if df.empty or "volume" not in df.columns:
        return None
    window = df.tail(lookback)
    if len(window) < 5:
        return None
    price_low = float(window["low"].min())
    price_high = float(window["high"].max())
    if price_high <= price_low:
        return None

    bins = _build_bins(price_low, price_high, n_bins)
    for _, row in window.iterrows():
        _distribute_candle(
            bins,
            candle_low=float(row["low"]),
            candle_high=float(row["high"]),
            candle_vol=float(row["volume"]),
        )

    total_vol = sum(b.volume for b in bins)
    if total_vol <= 0:
        return None

    # POC — bin with max volume
    poc_idx = max(range(len(bins)), key=lambda i: bins[i].volume)
    poc_bin = bins[poc_idx]

    # Expand around POC until we cover value_area_pct of total volume
    target = total_vol * value_area_pct
    selected = {poc_idx}
    covered = poc_bin.volume
    lo, hi = poc_idx, poc_idx
    while covered < target and (lo > 0 or hi < len(bins) - 1):
        next_lo = bins[lo - 1].volume if lo > 0 else -1.0
        next_hi = bins[hi + 1].volume if hi < len(bins) - 1 else -1.0
        if next_lo >= next_hi and lo > 0:
            lo -= 1
            covered += bins[lo].volume
            selected.add(lo)
        elif hi < len(bins) - 1:
            hi += 1
            covered += bins[hi].volume
            selected.add(hi)
        else:
            break

    val = bins[lo].low
    vah = bins[hi].high
    last_close = float(df["close"].iloc[-1])
    if last_close > vah:
        position = "above_va"
    elif last_close < val:
        position = "below_va"
    else:
        position = "inside_va"

    distance_to_poc_pct = (last_close / poc_bin.mid - 1.0) * 100.0 if poc_bin.mid else 0.0

    # HVN — top-3 by volume excluding POC
    ranked = sorted(range(len(bins)), key=lambda i: bins[i].volume, reverse=True)
    hvn_indices = [i for i in ranked if i != poc_idx][:3]
    hvn_prices = [round(float(bins[i].mid), 6) for i in hvn_indices]

    # LVN — lowest-volume bins inside the value area
    inside = [i for i in selected if i not in {poc_idx} and bins[i].volume > 0]
    inside.sort(key=lambda i: bins[i].volume)
    lvn_prices = [round(float(bins[i].mid), 6) for i in inside[:3]]

    bin_size = (price_high - price_low) / n_bins
    return {
        "poc": round(float(poc_bin.mid), 6),
        "vah": round(float(vah), 6),
        "val": round(float(val), 6),
        "bin_size": round(float(bin_size), 8),
        "n_bins": int(n_bins),
        "lookback_bars": int(len(window)),
        "position": position,
        "distance_to_poc_pct": round(float(distance_to_poc_pct), 3),
        "hvn": hvn_prices,
        "lvn": lvn_prices,
        "bins": [
            {
                "price": round(float(b.mid), 6),
                "volume": round(float(b.volume), 6),
                "share": round(float(b.volume / total_vol), 6),
            }
            for b in bins
        ],
    }


def volume_profile_summary_for_prompt(vp: dict | None) -> str:
    """Short bullet form for the LLM prompt."""
    if not vp:
        return ""
    pos_ru = {
        "above_va": "выше зоны стоимости (бычий перекос)",
        "below_va": "ниже зоны стоимости (медвежий перекос)",
        "inside_va": "внутри зоны стоимости",
    }.get(vp.get("position", ""), vp.get("position", ""))
    return (
        f"  • POC {vp['poc']}, VAH {vp['vah']}, VAL {vp['val']} (окно {vp['lookback_bars']} баров)\n"
        f"  • цена {pos_ru}, отклонение от POC {vp['distance_to_poc_pct']}%"
    )

"""Render an annotated candlestick chart and return PNG bytes."""
from __future__ import annotations

import base64
import io
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd

from .indicators import IndicatorBundle
from .schemas import Signal


def _format_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}".replace(",", " ")
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.4f}"


def render_chart(
    coin: str,
    timeframe: str,
    ind: IndicatorBundle,
    signal: Optional[Signal] = None,
    bars: int = 150,
    patterns: Optional[list[dict]] = None,
) -> bytes:
    df = ind.df.tail(bars).copy()
    df.index = pd.to_datetime(df.index).tz_convert(None)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})

    ema_fast = ind.ema_fast.tail(bars)
    ema_slow = ind.ema_slow.tail(bars)
    ema_long = ind.ema_long.tail(bars)
    bb_l = ind.bb["bb_lower"].tail(bars)
    bb_u = ind.bb["bb_upper"].tail(bars)
    rsi_s = ind.rsi.tail(bars)
    macd_line = ind.macd["macd"].tail(bars)
    macd_sig = ind.macd["signal"].tail(bars)
    macd_hist = ind.macd["hist"].tail(bars)

    addplots = [
        mpf.make_addplot(ema_fast.values, color="#2962ff", width=1.2, panel=0),
        mpf.make_addplot(ema_slow.values, color="#ff9800", width=1.2, panel=0),
        mpf.make_addplot(ema_long.values, color="#9c27b0", width=1.2, panel=0),
        mpf.make_addplot(bb_u.values, color="#888", width=0.8, linestyle="--", panel=0),
        mpf.make_addplot(bb_l.values, color="#888", width=0.8, linestyle="--", panel=0),
        mpf.make_addplot(rsi_s.values, color="#e91e63", width=1.2, panel=2, ylabel="RSI"),
        mpf.make_addplot(macd_line.values, color="#2962ff", width=1.0, panel=3, ylabel="MACD"),
        mpf.make_addplot(macd_sig.values, color="#ff9800", width=1.0, panel=3),
        mpf.make_addplot(
            macd_hist.values,
            type="bar",
            color=["#26a69a" if v >= 0 else "#ef5350" for v in macd_hist.values],
            panel=3,
            alpha=0.6,
        ),
    ]

    # Pattern marker overlays (one scatter per bias). mplfinance scatter
    # addplots require numeric arrays — use NaN for gaps, not Python None.
    n_bars = len(df)
    nan = float("nan")
    bull_marks: list[float] = [nan] * n_bars
    bear_marks: list[float] = [nan] * n_bars
    neutral_marks: list[float] = [nan] * n_bars
    pattern_label: Optional[tuple[int, str, str]] = None  # (bar_pos, name_ru, bias)
    if patterns:
        ranked = sorted(
            patterns,
            key=lambda p: (p.get("strength", 1), -abs(p.get("bar_index", -1))),
            reverse=True,
        )
        for p in patterns:
            bar_index = int(p.get("bar_index", -1))  # negative offset from end
            pos = n_bars + bar_index
            if pos < 0 or pos >= n_bars:
                continue
            bias = p.get("bias", "neutral")
            high = float(df["High"].iloc[pos])
            low = float(df["Low"].iloc[pos])
            pad = (high - low) * 0.6 if high > low else high * 0.002
            if bias == "bullish":
                v = low - pad
                cur = bull_marks[pos]
                bull_marks[pos] = v if np.isnan(cur) else min(cur, v)
            elif bias == "bearish":
                v = high + pad
                cur = bear_marks[pos]
                bear_marks[pos] = v if np.isnan(cur) else max(cur, v)
            else:
                v = high + pad
                cur = neutral_marks[pos]
                neutral_marks[pos] = v if np.isnan(cur) else max(cur, v)
        if ranked:
            top = ranked[0]
            top_pos = n_bars + int(top.get("bar_index", -1))
            if 0 <= top_pos < n_bars:
                pattern_label = (top_pos, top.get("name_ru", top.get("name", "")), top.get("bias", "neutral"))

        if any(not np.isnan(v) for v in bull_marks):
            addplots.append(
                mpf.make_addplot(
                    bull_marks,
                    type="scatter",
                    marker="^",
                    markersize=80,
                    color="#26a69a",
                    panel=0,
                )
            )
        if any(not np.isnan(v) for v in bear_marks):
            addplots.append(
                mpf.make_addplot(
                    bear_marks,
                    type="scatter",
                    marker="v",
                    markersize=80,
                    color="#ef5350",
                    panel=0,
                )
            )
        if any(not np.isnan(v) for v in neutral_marks):
            addplots.append(
                mpf.make_addplot(
                    neutral_marks,
                    type="scatter",
                    marker="o",
                    markersize=55,
                    color="#9e9e9e",
                    panel=0,
                )
            )

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit", wick="inherit", volume="in")
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mc,
        gridstyle=":",
        gridcolor="#444",
        facecolor="#0e1117",
        edgecolor="#0e1117",
        figcolor="#0e1117",
        rc={"axes.labelcolor": "#ddd", "xtick.color": "#aaa", "ytick.color": "#aaa", "axes.edgecolor": "#444"},
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        addplot=addplots,
        volume=True,
        panel_ratios=(6, 1.6, 2, 2),
        figsize=(13, 9),
        returnfig=True,
        tight_layout=True,
        xrotation=15,
        datetime_format="%m-%d %H:%M",
    )

    ax_main = axes[0]
    ax_rsi = axes[4] if len(axes) > 4 else None  # nightclouds-style: main, vol, rsi, macd

    # Title
    fig.suptitle(
        f"{coin}/USDT  •  {timeframe}",
        color="#fff",
        fontsize=14,
        fontweight="bold",
        y=0.985,
    )

    # Support / Resistance horizontal lines
    for lvl in ind.resistance[:3]:
        ax_main.axhline(lvl, color="#ef5350", linewidth=0.9, linestyle="--", alpha=0.85)
        ax_main.text(
            len(df) - 1,
            lvl,
            f" R: {_format_price(lvl)}",
            color="#ef5350",
            fontsize=8,
            va="center",
            ha="left",
        )
    for lvl in ind.support[:3]:
        ax_main.axhline(lvl, color="#26a69a", linewidth=0.9, linestyle="--", alpha=0.85)
        ax_main.text(
            len(df) - 1,
            lvl,
            f" S: {_format_price(lvl)}",
            color="#26a69a",
            fontsize=8,
            va="center",
            ha="left",
        )

    # Signal annotations (entry, SL, TP)
    if signal and signal.direction in {"long", "short"}:
        last_idx = len(df) - 1
        if signal.entry:
            ax_main.axhline(signal.entry, color="#ffeb3b", linewidth=1.2, linestyle="-", alpha=0.9)
            ax_main.text(
                last_idx,
                signal.entry,
                f" ENTRY {_format_price(signal.entry)}",
                color="#ffeb3b",
                fontsize=9,
                fontweight="bold",
                va="center",
                ha="left",
            )
        if signal.stop_loss:
            ax_main.axhline(signal.stop_loss, color="#ff5252", linewidth=1.0, linestyle=":", alpha=0.9)
            ax_main.text(
                last_idx,
                signal.stop_loss,
                f" SL {_format_price(signal.stop_loss)}",
                color="#ff5252",
                fontsize=9,
                fontweight="bold",
                va="center",
                ha="left",
            )
        if signal.take_profit_1:
            ax_main.axhline(signal.take_profit_1, color="#69f0ae", linewidth=1.0, linestyle=":", alpha=0.9)
            ax_main.text(
                last_idx,
                signal.take_profit_1,
                f" TP1 {_format_price(signal.take_profit_1)}",
                color="#69f0ae",
                fontsize=9,
                fontweight="bold",
                va="center",
                ha="left",
            )
        if signal.take_profit_2:
            ax_main.axhline(signal.take_profit_2, color="#00e676", linewidth=1.0, linestyle=":", alpha=0.9)
            ax_main.text(
                last_idx,
                signal.take_profit_2,
                f" TP2 {_format_price(signal.take_profit_2)}",
                color="#00e676",
                fontsize=9,
                fontweight="bold",
                va="center",
                ha="left",
            )
        # Direction badge
        direction_label = "LONG" if signal.direction == "long" else "SHORT"
        color = "#26a69a" if signal.direction == "long" else "#ef5350"
        ax_main.text(
            0.01,
            0.97,
            f"{direction_label}  •  conf {signal.confidence}%",
            transform=ax_main.transAxes,
            color="#fff",
            fontsize=11,
            fontweight="bold",
            va="top",
            bbox=dict(facecolor=color, edgecolor="none", boxstyle="round,pad=0.4", alpha=0.9),
        )

    # Label for the most significant recent pattern
    if pattern_label is not None:
        pos, name_ru, bias = pattern_label
        color_map = {"bullish": "#26a69a", "bearish": "#ef5350", "neutral": "#9e9e9e"}
        color = color_map.get(bias, "#9e9e9e")
        try:
            high = float(df["High"].iloc[pos])
            low = float(df["Low"].iloc[pos])
            offset = (high - low) * 1.4 if high > low else high * 0.005
            y = (low - offset) if bias == "bullish" else (high + offset)
            ax_main.annotate(
                name_ru,
                xy=(pos, high if bias != "bullish" else low),
                xytext=(pos, y),
                fontsize=9,
                color=color,
                fontweight="bold",
                ha="center",
                arrowprops=dict(arrowstyle="-", color=color, lw=0.8, alpha=0.7),
            )
        except Exception:  # noqa: BLE001
            pass

    # RSI 30/70 lines
    if ax_rsi is not None:
        ax_rsi.axhline(70, color="#ef5350", linewidth=0.7, linestyle="--", alpha=0.6)
        ax_rsi.axhline(30, color="#26a69a", linewidth=0.7, linestyle="--", alpha=0.6)
        ax_rsi.set_ylim(0, 100)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor="#0e1117", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def render_chart_b64(*args, **kwargs) -> str:
    return base64.b64encode(render_chart(*args, **kwargs)).decode("ascii")

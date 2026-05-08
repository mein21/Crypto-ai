"""Unit tests for the sentiment composite."""
from __future__ import annotations

import pandas as pd
import pytest

from app.sentiment import compute_sentiment, sentiment_summary_for_prompt


def test_neutral_when_no_inputs():
    res = compute_sentiment(fear_greed=None, news=None, daily_close=None)
    assert 45 <= res["score"] <= 55
    assert "components" in res


def test_extreme_greed_pulls_score_up():
    fg = {"value": 90, "classification": "Extreme Greed"}
    news = [{"title": "BTC rally accelerates as ETF approval boosts adoption"}]
    closes = pd.Series([100.0, 105.0])  # +5%
    res = compute_sentiment(fear_greed=fg, news=news, daily_close=closes)
    assert res["score"] >= 70
    assert "бычий" in res["label"] or "слабо бычий" in res["label"]
    assert res["components"]["fear_greed"] == 90.0
    assert res["components"]["momentum_pct"] == 5.0


def test_extreme_fear_pulls_score_down():
    fg = {"value": 10, "classification": "Extreme Fear"}
    news = [
        {"title": "Major exchange hack, billions in liquidations"},
        {"title": "SEC sues issuer, ban looms"},
    ]
    closes = pd.Series([100.0, 92.0])
    res = compute_sentiment(fear_greed=fg, news=news, daily_close=closes)
    assert res["score"] <= 30
    assert res["components"]["news_breakdown"]["bearish_hits"] >= 2


def test_balanced_news_keeps_news_score_neutral():
    res = compute_sentiment(
        fear_greed=None,
        news=[{"title": "rally and crash mixed signals"}],
        daily_close=None,
    )
    # Balanced bull/bear hits → news = 50
    assert 45 <= res["components"]["news"] <= 55


def test_summary_renders_for_prompt():
    res = compute_sentiment(
        fear_greed={"value": 60},
        news=[{"title": "support rebound"}],
        daily_close=pd.Series([100.0, 102.0]),
    )
    s = sentiment_summary_for_prompt(res)
    assert "sentiment" in s.lower() or "композит" in s.lower()


def test_summary_handles_none():
    assert sentiment_summary_for_prompt(None) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

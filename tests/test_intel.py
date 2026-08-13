from datetime import datetime, timedelta, timezone

import pytest

from shunkan.intel.feeds import NewsItem
from shunkan.intel.impact import aggregate_bias, assess_impact
from shunkan.intel.sentiment import score_sentiment, sentiment_label
from shunkan.intel.summarize import summarize
from shunkan.markets import IST, session_phase


def _item(title: str, hours_ago: float = 1.0, desc: str = "") -> NewsItem:
    return NewsItem(
        title=title,
        link="",
        source="Test Wire",
        published=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        description=desc,
    )


# -- sentiment ---------------------------------------------------------------


def test_sentiment_bullish():
    assert score_sentiment("Nifty surges to record high as banks rally sharply") > 0.3


def test_sentiment_bearish():
    assert score_sentiment("Sensex crashes 1,200 points as FII selloff deepens") < -0.3


def test_sentiment_neutral():
    assert abs(score_sentiment("NSE to revise lot sizes from next quarter")) < 0.12


def test_sentiment_rate_cut_is_bullish():
    assert score_sentiment("RBI cuts repo rate by 25 bps") > 0.1


def test_sentiment_guidance_cut_is_bearish():
    assert score_sentiment("Infosys cuts revenue guidance for FY27") < 0


def test_sentiment_labels():
    assert sentiment_label(0.5) == "bullish"
    assert sentiment_label(-0.5) == "bearish"
    assert sentiment_label(0.0) == "neutral"


# -- summarizer ---------------------------------------------------------------


def test_summarize_picks_central_sentences():
    text = (
        "The RBI kept the repo rate unchanged at its June meeting. "
        "The central bank cited sticky food inflation as the key concern. "
        "Markets had widely expected this decision. "
        "Bank Nifty rose modestly after the announcement. "
        "Analysts now expect the first cut in the December quarter."
    )
    out = summarize(text, max_sentences=2)
    assert len(out) < len(text)
    assert out.count(".") <= 3


def test_summarize_short_text_passthrough():
    assert summarize("One short line.") == "One short line."
    assert summarize("") == ""


# -- impact -------------------------------------------------------------------


def test_impact_rbi_news_categorized():
    call = assess_impact(_item("RBI cuts repo rate by 50 bps in surprise move"))
    assert call.category == "rbi_policy"
    assert call.direction == "bullish"
    assert 0.5 <= call.confidence <= 0.85


def test_impact_oil_surge_is_inverse_for_equities():
    call = assess_impact(_item("Brent crude surges 8% as OPEC slashes output"))
    assert call.category == "crude_oil"
    assert call.direction == "bearish"  # oil up = bad for Indian equities


def test_impact_overnight_news_targets_open():
    overnight = datetime(2026, 6, 10, 23, 30, tzinfo=IST)  # Wednesday night IST
    item = _item("Fed signals dovish pivot, Wall Street rallies")
    item.published = overnight.astimezone(timezone.utc)
    call = assess_impact(item)
    assert call.phase == "overnight"
    assert "open" in call.horizon


def test_impact_confidence_capped():
    call = assess_impact(_item("Massive record surge: everything soars hugely"))
    assert call.confidence <= 0.85


def test_aggregate_bias_direction():
    bullish_items = [
        _item("Nifty surges as FII inflows hit record"),
        _item("Banks rally sharply on strong credit growth"),
        _item("RBI cuts rates, markets jump"),
    ]
    bias = aggregate_bias(bullish_items)
    assert bias.score > 0
    assert "bullish" in bias.label
    assert len(bias.drivers) >= 1


def test_aggregate_bias_empty():
    bias = aggregate_bias([])
    assert bias.label == "no data"


def test_aggregate_recency_decay():
    fresh_bear = [_item("Markets crash on war fears", hours_ago=0.5)]
    stale_bull = [_item("Markets soar to record highs", hours_ago=72.0)]
    bias = aggregate_bias(fresh_bear + stale_bull)
    assert bias.score < 0  # the fresh bearish item dominates


# -- sessions -------------------------------------------------------------------


def test_session_phases():
    monday_open = datetime(2026, 6, 8, 9, 30, tzinfo=IST)
    assert session_phase(monday_open).phase == "opening"
    assert session_phase(monday_open).is_open

    saturday = datetime(2026, 6, 13, 11, 0, tzinfo=IST)
    assert not session_phase(saturday).is_open

    closing = datetime(2026, 6, 8, 15, 0, tzinfo=IST)
    assert session_phase(closing).phase == "closing"

    late = datetime(2026, 6, 8, 22, 0, tzinfo=IST)
    assert session_phase(late).phase == "overnight"

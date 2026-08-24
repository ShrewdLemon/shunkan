"""News → probable market impact, conditioned on WHEN the news lands.

The same headline moves markets differently at different times: hawkish Fed
news at 2 AM IST shapes tomorrow's opening gap; an RBI surprise at 11 AM
hits Bank Nifty within minutes; anything after 15:30 carries to the next
session. This module classifies each headline (category taxonomy), scores
its sentiment, maps its timestamp to an IST session phase, and emits a
probable direction + confidence + horizon + affected segment.

These are transparent, explainable heuristics for decision support — NOT
trade signals. Confidence is deliberately capped; markets routinely do the
opposite of the obvious read.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shunkan.intel.feeds import NewsItem
from shunkan.intel.sentiment import score_sentiment, sentiment_label
from shunkan.markets import IST, session_phase

# category -> (regex, base_weight 0..1, affected segment, sentiment_flip)
# sentiment_flip = -1 where "positive words" are bad for equities (e.g. "oil surges").
_CATEGORIES: list[tuple[str, re.Pattern, float, str, int]] = [
    ("rbi_policy", re.compile(r"\brbi\b|\bmpc\b|repo rate|monetary policy", re.I),
     0.95, "index + banks", 1),
    ("global_cues", re.compile(r"\bfed\b|fomc|wall street|nasdaq|dow|s&p|treasury|us market|powell", re.I),
     0.80, "index (gap risk)", 1),
    ("fii_flows", re.compile(r"\bfii\b|\bdii\b|foreign (portfolio )?investor|fpi", re.I),
     0.75, "index", 1),
    ("inflation_macro", re.compile(r"inflation|\bcpi\b|\bwpi\b|\bgdp\b|\biip\b|\bpmi\b|fiscal deficit", re.I),
     0.70, "index", 1),
    ("crude_oil", re.compile(r"crude|brent|\bopec\b|oil price", re.I),
     0.60, "OMCs, paints, aviation (index inverse)", -1),
    ("regulation_tax", re.compile(r"\bsebi\b|\bgst\b|budget|taxation|tariff|import duty|export ban", re.I),
     0.65, "policy-sensitive sectors", 1),
    ("geopolitics", re.compile(r"\bwar\b|border|missile|geopolit|election|attack|conflict", re.I),
     0.65, "index (risk-off)", 1),
    ("earnings", re.compile(r"\bq[1-4]\b|results|earnings|net profit|revenue|ebitda|guidance", re.I),
     0.55, "stock-specific", 1),
    ("corporate_action", re.compile(r"merger|acquisition|stake|buyback|\bipo\b|delisting|bonus issue|split", re.I),
     0.45, "stock-specific", 1),
]

# session phase -> (impact multiplier, horizon text)
_TIMING: dict[str, tuple[float, str]] = {
    "overnight":   (1.15, "gap at next 09:15 open"),
    "post_market": (1.05, "priced into tomorrow's open"),
    "pre_open":    (1.20, "imminent — opening minutes"),
    "opening":     (1.10, "intraday, first hour"),
    "midday":      (0.90, "intraday drift"),
    "closing":     (0.95, "late-day move, spillover tomorrow"),
    # CAS, live since 2026-08-03. News landing while F&O cash names are in
    # the call auction cannot be traded in the cash market at all, and the
    # derivatives tail is thin - both get discounted rather than crashing
    # the lookup, which is what a missing key did here.
    "auction":       (0.70, "closing auction — cash in call, F&O only"),
    "closing_deriv": (0.80, "derivatives tail to 15:40, cash closed"),
}


def timing_factor(phase: str) -> tuple[float, str]:
    """Never raise on an unknown phase. The session model gains phases as
    the exchange changes its rules; an unrecognised one should degrade to
    neutral, not take the news panel down with it."""
    return _TIMING.get(phase, (1.0, "outside modelled sessions"))


@dataclass
class ImpactCall:
    category: str
    direction: str          # "bullish" / "bearish" / "neutral"
    confidence: float       # 0..1, capped at 0.85
    horizon: str            # when the move would likely express
    segment: str            # what it hits
    magnitude: str          # qualitative band
    phase: str              # IST session phase when news landed
    rationale: str
    components: dict = field(default_factory=dict)  # the arithmetic, for provenance


@dataclass
class MarketBias:
    score: float            # -1..+1 net weighted bias
    label: str
    n_items: int
    drivers: list[str] = field(default_factory=list)
    gap_call: str = ""      # explicit next-open read when overnight news dominates


def assess_impact(item: NewsItem, now: datetime | None = None) -> ImpactCall:
    text = f"{item.title}. {item.description}"
    sent = item.sentiment if item.sentiment else score_sentiment(text)
    item.sentiment = sent

    category, weight, segment, flip = "other", 0.30, "broad market", 1
    for name, pattern, w, seg, fl in _CATEGORIES:
        if pattern.search(text):
            category, weight, segment, flip = name, w, seg, fl
            break

    effective = sent * flip
    published = item.published or (now or datetime.now(timezone.utc))
    phase = session_phase(published.astimezone(IST))
    timing_mult, horizon = timing_factor(phase.phase)

    strength = abs(effective) * weight * timing_mult
    confidence = min(0.5 + 0.45 * strength, 0.85)
    direction = (
        "bullish" if effective > 0.08 else "bearish" if effective < -0.08 else "neutral"
    )
    if direction == "neutral":
        confidence = min(confidence, 0.5)

    if strength > 0.45:
        magnitude = "large (index-moving)"
    elif strength > 0.25:
        magnitude = "moderate"
    else:
        magnitude = "small"

    rationale = (
        f"{category.replace('_', ' ')} · sentiment {sentiment_label(sent)} ({sent:+.2f})"
        + (" · inverse for equities" if flip == -1 and abs(sent) > 0.05 else "")
        + f" · landed {phase.phase.replace('_', ' ')} IST → {horizon}"
    )
    return ImpactCall(
        category=category,
        direction=direction,
        confidence=confidence,
        horizon=horizon,
        segment=segment,
        magnitude=magnitude,
        phase=phase.phase,
        rationale=rationale,
        components={
            "sentiment": round(sent, 4),
            "category_weight": weight,
            "sentiment_flip": flip,
            "timing_multiplier": timing_mult,
            "strength": round(strength, 4),
            "formula": "conf = min(0.5 + 0.45·|sent·flip|·w_cat·w_time, 0.85)",
        },
    )


def aggregate_bias(items: list[NewsItem], now: datetime | None = None) -> MarketBias:
    """Net directional read across the feed, recency-decayed (6h half-life)."""
    if not items:
        return MarketBias(score=0.0, label="no data", n_items=0)

    total, weight_sum = 0.0, 0.0
    overnight_score = 0.0
    drivers: list[tuple[float, str]] = []
    for item in items:
        call = item.impact.get("_call") if isinstance(item.impact, dict) else None
        if not isinstance(call, ImpactCall):
            call = assess_impact(item, now=now)
            item.impact["_call"] = call
        sign = 1.0 if call.direction == "bullish" else -1.0 if call.direction == "bearish" else 0.0
        decay = math.pow(0.5, item.age_hours / 6.0)
        contrib = sign * call.confidence * decay
        total += contrib
        weight_sum += call.confidence * decay
        if call.phase in ("overnight", "post_market"):
            overnight_score += contrib
        if sign != 0.0:
            drivers.append((abs(contrib), f"{call.direction} · {item.title[:70]}"))

    score = total / weight_sum if weight_sum > 0 else 0.0
    label = (
        "bullish" if score > 0.25 else "leaning bullish" if score > 0.08
        else "bearish" if score < -0.25 else "leaning bearish" if score < -0.08
        else "neutral / mixed"
    )
    drivers.sort(key=lambda d: -d[0])

    gap_call = ""
    if abs(overnight_score) > 0.4:
        gap_dir = "gap-up" if overnight_score > 0 else "gap-down"
        gap_call = f"Overnight flow favors a {gap_dir} open (heuristic, not a guarantee)"

    return MarketBias(
        score=score,
        label=label,
        n_items=len(items),
        drivers=[d[1] for d in drivers[:5]],
        gap_call=gap_call,
    )

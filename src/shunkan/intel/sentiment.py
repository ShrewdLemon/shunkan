"""Financial-news sentiment scoring.

A finance-tuned lexicon (Loughran-McDonald-style word classes plus
India-market vocabulary) with negation and intensifier handling. Pure
Python over small dicts — scores a headline in ~10 microseconds, so the
whole feed is scored in under a millisecond. No model download, no API.
"""

from __future__ import annotations

import re

POSITIVE = {
    "gain", "gains", "gained", "rally", "rallies", "rallied", "surge", "surges",
    "surged", "soar", "soars", "soared", "jump", "jumps", "jumped", "rise",
    "rises", "rose", "rising", "up", "upbeat", "beat", "beats", "record",
    "high", "highs", "strong", "stronger", "strength", "boost", "boosts",
    "boosted", "bullish", "outperform", "outperforms", "upgrade", "upgrades",
    "upgraded", "buy", "accumulate", "profit", "profits", "growth", "grows",
    "expands", "expansion", "recovery", "recovers", "rebound", "rebounds",
    "positive", "optimism", "optimistic", "advance", "advances", "climb",
    "climbs", "climbed", "inflow", "inflows", "easing", "eases", "cut",
    "cuts", "dovish", "stimulus", "winner", "winners", "top", "robust",
}
# Note: "cut"/"cuts" treated positive only in rate context (see _RATE_RE).

NEGATIVE = {
    "fall", "falls", "fell", "fallen", "drop", "drops", "dropped", "plunge",
    "plunges", "plunged", "crash", "crashes", "crashed", "slump", "slumps",
    "slumped", "sink", "sinks", "sank", "slide", "slides", "slid", "down",
    "decline", "declines", "declined", "weak", "weaker", "weakness", "loss",
    "losses", "lost", "bearish", "downgrade", "downgrades", "downgraded",
    "sell", "selloff", "sell-off", "miss", "misses", "missed", "low", "lows",
    "fear", "fears", "concern", "concerns", "worry", "worries", "risk",
    "risks", "pressure", "outflow", "outflows", "hawkish", "hike", "hikes",
    "inflation", "deficit", "fraud", "probe", "penalty", "default", "war",
    "tension", "tensions", "tariff", "tariffs", "sanctions", "recession",
    "volatile", "volatility", "uncertainty", "crisis", "shutdown", "strike",
    "laggard", "laggards", "tumble", "tumbles", "tumbled", "rout", "panic",
}

NEGATORS = {"not", "no", "without", "despite", "unlikely", "halt", "halts", "snap", "snaps"}
INTENSIFIERS = {"sharply", "strongly", "massively", "heavily", "steep", "steeply", "big", "huge", "major"}

_TOKEN_RE = re.compile(r"[a-z']+")
_RATE_RE = re.compile(r"\brate[s]?\b|\brepo\b|\brbi\b|\bfed\b")


def score_sentiment(text: str) -> float:
    """Score text in [-1, 1]: -1 strongly bearish, +1 strongly bullish."""
    return score_sentiment_detailed(text)["score"]


def score_sentiment_detailed(text: str) -> dict:
    """Score plus the term-level evidence (for provenance display)."""
    lower = text.lower()
    tokens = _TOKEN_RE.findall(lower)
    if not tokens:
        return {"score": 0.0, "pos_terms": [], "neg_terms": [], "hits": 0}
    rate_context = bool(_RATE_RE.search(lower))

    score = 0.0
    hits = 0
    pos_terms: list[str] = []
    neg_terms: list[str] = []
    for i, tok in enumerate(tokens):
        weight = 0.0
        if tok in POSITIVE:
            # "cut" is bullish only for rates ("RBI cuts repo rate"), bearish
            # for forecasts/jobs ("cuts guidance", "cuts jobs").
            if tok in ("cut", "cuts") and not rate_context:
                weight = -1.0
            else:
                weight = 1.0
        elif tok in NEGATIVE:
            # Rate hikes are the canonical hawkish negative; keep as-is.
            weight = -1.0
        if weight == 0.0:
            continue
        window = tokens[max(0, i - 3): i]
        if any(w in NEGATORS for w in window):
            weight = -weight * 0.8
        if any(w in INTENSIFIERS for w in window):
            weight *= 1.5
        (pos_terms if weight > 0 else neg_terms).append(tok)
        score += weight
        hits += 1

    if hits == 0:
        return {"score": 0.0, "pos_terms": [], "neg_terms": [], "hits": 0}
    # Normalize by hits with diminishing returns; clamp to [-1, 1].
    raw = score / (hits ** 0.7)
    return {
        "score": max(-1.0, min(1.0, raw * 0.6)),
        "pos_terms": pos_terms,
        "neg_terms": neg_terms,
        "hits": hits,
    }


def sentiment_label(score: float) -> str:
    if score >= 0.35:
        return "bullish"
    if score >= 0.12:
        return "mildly bullish"
    if score <= -0.35:
        return "bearish"
    if score <= -0.12:
        return "mildly bearish"
    return "neutral"

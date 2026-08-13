"""Extractive summarization — TextRank-lite over sentence similarity.

Picks the most central sentences by TF overlap. Pure Python/numpy, no
model download; summarizing a full article body takes single-digit
milliseconds. For headlines-only feeds it condenses the description.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD_RE = re.compile(r"[a-z']{3,}")

_STOP = set(
    """the and for are but not you all any can her was one our out day get has him
    his how man new now old see two way who boy did its let put say she too use
    that this with from they will have been were said each which their time would
    there what about into more other could than then them these some only over
    after also just most such where through before between during under while""".split()
)


def _tokenize(sentence: str) -> Counter:
    return Counter(w for w in _WORD_RE.findall(sentence.lower()) if w not in _STOP)


def _similarity(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = sum((a & b).values())
    if common == 0:
        return 0.0
    return common / (math.log(1 + sum(a.values())) + math.log(1 + sum(b.values())))


def summarize(text: str, max_sentences: int = 2) -> str:
    """Return the most central `max_sentences` sentences in original order."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) > 20]
    if len(sentences) <= max_sentences:
        return text if len(sentences) <= 1 else " ".join(sentences[:max_sentences])

    tokens = [_tokenize(s) for s in sentences]
    n = len(sentences)
    scores = [0.0] * n
    for i in range(n):
        for j in range(n):
            if i != j:
                scores[i] += _similarity(tokens[i], tokens[j])

    ranked = sorted(range(n), key=lambda i: -scores[i])[:max_sentences]
    return " ".join(sentences[i] for i in sorted(ranked))

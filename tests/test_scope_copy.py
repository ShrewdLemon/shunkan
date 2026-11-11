"""User-facing copy never claims a speed or automation Shunkan does not have."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["README.md", "pyproject.toml", "src/shunkan/server/static/index.html",
         "src/shunkan/server/static/app.js", "src/shunkan/server/static/manifest.json"]
CLAIMS = re.compile(r"\bHFTs?\b|high[- ]frequency|scalp|fast[- ]paced|ultra[- ]low[- ]latency"
                    r"|automated trading|auto[- ]?trad(e|ing)|algo[- ]trading platform", re.I)


def test_no_speed_or_automation_claims():
    hits = [f"{f}:{n}: {line.strip()[:90]}"
            for f in FILES
            for n, line in enumerate((ROOT / f).read_text(encoding="utf-8").splitlines(), 1)
            if CLAIMS.search(line)]
    assert not hits, "\n".join(hits)

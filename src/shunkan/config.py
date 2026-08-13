"""User configuration and state persistence (~/.shunkan)."""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("SHUNKAN_HOME", Path.home() / ".shunkan"))
CACHE_DIR = APP_DIR / "cache"
WATCHLIST_FILE = APP_DIR / "watchlist.json"
PORTFOLIO_FILE = APP_DIR / "portfolio.json"

# India-first: NSE large caps + the two index futures underlyings.
DEFAULT_WATCHLIST = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "BHARTIARTL", "LT", "NIFTY", "BANKNIFTY",
]


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_watchlist() -> list[str]:
    ensure_dirs()
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text())
            if isinstance(data, list) and data:
                return [str(s).upper() for s in data]
        except (json.JSONDecodeError, OSError):
            pass
    return list(DEFAULT_WATCHLIST)


def save_watchlist(symbols: list[str]) -> None:
    ensure_dirs()
    WATCHLIST_FILE.write_text(json.dumps(sorted(set(s.upper() for s in symbols)), indent=2))

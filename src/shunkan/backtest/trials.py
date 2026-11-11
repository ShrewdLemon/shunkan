"""Every configuration run by hand on a symbol is a trial.

The deflated Sharpe is only honest when it counts every look at the data, not
just the one the user chose to validate. The ledger keeps one entry per
distinct configuration per symbol, across sessions; re-running the same
configuration does not add a trial.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from pathlib import Path

from shunkan.backtest.validate import SearchTrials


def _norm(v):
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def fingerprint(config: dict) -> str:
    """20 and 20.0 are the same configuration."""
    raw = json.dumps(_norm(config), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _sharpe(v) -> float | None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None


class TrialLedger:
    """symbol -> {fingerprint: sharpe or None}. `path=None` keeps it in memory."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, float | None]] = self._load()

    def _load(self) -> dict:
        if self._path is None or not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(sym): {str(fp): _sharpe(v) for fp, v in runs.items()}
                for sym, runs in data.items() if isinstance(runs, dict)}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._runs, sort_keys=True))
        tmp.replace(self._path)

    def record(self, symbol: str, config: dict, sharpe: float | None = None) -> int:
        sym = symbol.upper()
        fp = fingerprint(config)
        val = _sharpe(sharpe)
        with self._lock:
            runs = self._runs.setdefault(sym, {})
            if fp not in runs or (runs[fp] is None and val is not None):
                runs[fp] = val
                self._save()
            return len(runs)

    def count(self, symbol: str) -> int:
        with self._lock:
            return len(self._runs.get(symbol.upper(), {}))

    def trials(self, symbol: str) -> SearchTrials:
        sym = symbol.upper()
        with self._lock:
            runs = dict(self._runs.get(sym, {}))
        n = max(len(runs), 1)
        sharpes = [s for s in runs.values() if s is not None]
        std = None
        if len(sharpes) > 1:
            mu = sum(sharpes) / len(sharpes)
            sd = math.sqrt(sum((s - mu) ** 2 for s in sharpes) / (len(sharpes) - 1))
            std = max(1.0, sd)
        noun = "configuration" if n == 1 else "configurations"
        return SearchTrials(n_trials=n, sharpe_std=std,
                            source=f"{n} {noun} run on {sym} in this app")

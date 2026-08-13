"""Price/technical alerts with persistence and desktop notifications.

Grammar (same in TUI and CLI):

    alert NIFTY > 23500          price level
    alert VIX > 18               India VIX threshold
    alert RELIANCE rsi < 30      RSI(14) on daily bars
    alert SBIN vol_surge > 2     volume vs 20-day average

Alerts persist in ~/.shunkan/alerts.json, are checked on a timer while the
terminal runs, fire once (then auto-disarm), and raise both an in-app
notification and a macOS banner.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shunkan.config import APP_DIR, ensure_dirs

ALERTS_FILE = APP_DIR / "alerts.json"

METRICS = ("price", "rsi", "vol_surge")
_RULE_RE = re.compile(
    r"^\s*(?P<sym>[A-Za-z0-9&._^=-]+)\s+(?:(?P<metric>rsi|vol_surge)\s+)?"
    r"(?P<op><|>|<=|>=)\s*(?P<value>-?[\d.]+)\s*$"
)


@dataclass
class Alert:
    symbol: str
    metric: str  # price | rsi | vol_surge
    op: str      # < > <= >=
    value: float
    armed: bool = True
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    fired_at: str = ""
    fired_value: float = 0.0

    def describe(self) -> str:
        metric = "" if self.metric == "price" else f" {self.metric}"
        state = "armed" if self.armed else f"FIRED at {self.fired_value:g} ({self.fired_at[:16]})"
        return f"{self.symbol}{metric} {self.op} {self.value:g} — {state}"

    def check(self, current: float) -> bool:
        if self.op == ">":
            return current > self.value
        if self.op == "<":
            return current < self.value
        if self.op == ">=":
            return current >= self.value
        return current <= self.value


def parse_alert(text: str) -> Alert:
    """Parse 'NIFTY > 23500' / 'RELIANCE rsi < 30' / 'SBIN vol_surge > 2'."""
    m = _RULE_RE.match(text)
    if not m:
        raise ValueError(
            "Cannot parse alert. Examples: `alert NIFTY > 23500`, "
            "`alert RELIANCE rsi < 30`, `alert SBIN vol_surge > 2`"
        )
    return Alert(
        symbol=m.group("sym").upper(),
        metric=m.group("metric") or "price",
        op=m.group("op"),
        value=float(m.group("value")),
    )


class AlertBook:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ALERTS_FILE
        self.alerts: list[Alert] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.alerts = [Alert(**a) for a in data]
            except (json.JSONDecodeError, TypeError, OSError):
                self.alerts = []

    def save(self) -> None:
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(a) for a in self.alerts], indent=2))

    def add(self, alert: Alert) -> None:
        self.alerts.append(alert)
        self.save()

    def remove(self, index: int) -> Alert:
        if not 0 <= index < len(self.alerts):
            raise ValueError(f"No alert #{index + 1}. `alerts` lists them.")
        gone = self.alerts.pop(index)
        self.save()
        return gone

    @property
    def armed(self) -> list[Alert]:
        return [a for a in self.alerts if a.armed]

    def check_all(self, provider) -> list[tuple[Alert, float]]:
        """Evaluate armed alerts; returns the ones that fired this pass."""
        fired: list[tuple[Alert, float]] = []
        by_symbol: dict[str, list[Alert]] = {}
        for a in self.armed:
            by_symbol.setdefault(a.symbol, []).append(a)
        if not by_symbol:
            return fired

        price_syms = sorted(
            {s for s, alerts in by_symbol.items() if any(a.metric == "price" for a in alerts)}
        )
        prices: dict[str, float] = {}
        if price_syms:
            try:
                quotes = provider.quotes(price_syms)
                prices = {s: q.price for s, q in quotes.items()}
            except Exception:
                pass

        for sym, alerts in by_symbol.items():
            hist = None
            for alert in alerts:
                current = None
                if alert.metric == "price":
                    current = prices.get(sym)
                else:
                    if hist is None:
                        try:
                            hist = provider.history(sym, period="3mo", interval="1d")
                        except Exception:
                            continue
                    if alert.metric == "rsi":
                        from shunkan.analytics.indicators import rsi

                        series = rsi(hist["close"], 14).dropna()
                        current = float(series.iloc[-1]) if len(series) else None
                    elif alert.metric == "vol_surge":
                        vol = hist["volume"]
                        trail = vol.iloc[-21:-1]
                        if len(trail) > 2 and trail.mean() > 0:
                            current = float(vol.iloc[-1] / trail.mean())
                if current is None:
                    continue
                if alert.check(current):
                    alert.armed = False
                    alert.fired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    alert.fired_value = current
                    fired.append((alert, current))
        if fired:
            self.save()
        return fired


def desktop_notify(title: str, message: str) -> None:
    """Best-effort OS notification (macOS banner via osascript)."""
    if sys.platform != "darwin":
        return
    try:
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5, check=False,
        )
    except Exception:
        pass

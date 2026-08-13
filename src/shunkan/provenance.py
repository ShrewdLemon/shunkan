"""Provenance: every calculative score carries its own derivation.

A `prov` dict travels with a value through the API so the UI's ⓘ mark can
show exactly how the number was made: the formula, the real input values,
where each input came from, and when. If a score can't explain itself, it
shouldn't be on the screen.
"""

from __future__ import annotations

from datetime import datetime, timezone


def prov(
    formula: str,
    inputs: dict[str, object],
    source: str,
    method: str = "",
    caveat: str = "",
) -> dict:
    """Build a provenance record.

    inputs: name -> value, or name -> (value, sub-source) for inputs with
    their own origin (e.g. spot from Kite, window from config).
    """
    norm: list[dict] = []
    for name, v in inputs.items():
        if isinstance(v, tuple) and len(v) == 2:
            norm.append({"name": name, "value": _fmt(v[0]), "source": v[1]})
        else:
            norm.append({"name": name, "value": _fmt(v), "source": ""})
    return {
        "formula": formula,
        "inputs": norm,
        "source": source,
        "method": method,
        "caveat": caveat,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _fmt(v) -> str:
    if isinstance(v, float):
        if abs(v) >= 10000:
            return f"{v:,.0f}"
        if abs(v) >= 1:
            return f"{v:,.4g}"
        return f"{v:.4g}"
    return str(v)

"""`shunkan demo`: fill an empty store with public end-of-day data.

Ships no data and fabricates nothing: every row is fetched to this machine
from the public source the terminal already uses.
"""

from __future__ import annotations

DEMO_SYMBOLS = ("^NSEI", "^NSEBANK", "^INDIAVIX")


def run(days: int = 90, provider=None, archive=None, backfill=None, out=print) -> dict:
    if provider is None:
        from shunkan.data.provider import get_provider, is_offline
        if is_offline():
            msg = "SHUNKAN_OFFLINE is set; the demo needs live public sources"
            out(f"  {msg}")
            return {"history": {}, "participant": None, "failed": {"offline": msg}}
        provider = get_provider()
    if archive is None:
        from shunkan.store import HistoryArchive
        archive = HistoryArchive()
    if backfill is None:
        from shunkan.data.participant import backfill
    src = getattr(provider, "broker_name", "") or "yahoo/nse"
    history, failed = {}, {}
    for sym in DEMO_SYMBOLS:
        try:
            df = provider.history(sym, period="max", interval="1d")
            history[sym] = archive.upsert(sym, df, src)
            out(f"  {sym}: {history[sym]} daily rows")
        except Exception as exc:
            failed[sym] = f"{type(exc).__name__}: {exc}"[:160]
            out(f"  {sym}: FAILED {failed[sym]}")
    try:
        participant = backfill(days, progress=None)
        out(f"  NSE participant archive: {participant}")
        reasons = []
        if participant.get("failed"):
            reasons.append(f"{participant['failed']} day(s) failed to fetch")
        if participant.get("days_on_disk", 0) < 2:
            reasons.append(f"only {participant.get('days_on_disk', 0)} participant days on disk")
        if reasons:
            failed["participant"] = "; ".join(reasons)
    except Exception as exc:
        participant = None
        failed["participant"] = f"{type(exc).__name__}: {exc}"[:160]
        out(f"  NSE participant archive: FAILED {failed['participant']}")
    if failed:
        out(f"demo incomplete: {', '.join(failed)} failed")
    else:
        out("Next: `shunkan serve`, then type BRF NIFTY.")
    return {"history": history, "participant": participant, "failed": failed}

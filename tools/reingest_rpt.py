"""Re-ingest every stored BSE related-party filing onto the graph.

Resumable ON PURPOSE. The first version walked all 500 symbols from the top
every run, so a laptop closing at symbol 300 threw away twelve minutes of work
and re-did it. Each finished symbol is appended to a done-file and skipped on
the next run; --fresh forces the full pass.

Re-ingesting an already-ingested symbol is harmless (put_edges upserts on the
src/dst/rel key), so the done-file is an optimisation, not a correctness
guard - a half-written line costs one repeated symbol, nothing worse.
"""
from __future__ import annotations

import argparse
import pathlib
import time
import warnings

warnings.filterwarnings("ignore")

from shunkan.config import APP_DIR
from shunkan.data.bse import ingest_rpt, scrip_code
from shunkan.data.constituents import fetch_constituents

STATE = APP_DIR / "store" / "bse"
DONE = STATE / "rpt_ingested.txt"
LOG = STATE / "rpt_ingest.log"


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="NIFTY500")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the done-file and re-ingest everything")
    args = ap.parse_args()

    STATE.mkdir(parents=True, exist_ok=True)
    if args.fresh and DONE.exists():
        DONE.unlink()
    done = set(DONE.read_text().split()) if DONE.exists() else set()

    syms = [c.symbol for c in fetch_constituents(args.index)]
    todo = [s for s in syms if s not in done]
    say(f"START {len(todo)} to do, {len(done)} already ingested")

    ok = fail = skip = edges = 0
    for i, sym in enumerate(todo, 1):
        try:
            code = scrip_code(sym)
            src = STATE / f"rpt_{code}.json"
            if not src.exists():
                skip += 1
                continue
            res = ingest_rpt(code, symbol=sym)
            ok += 1
            edges += res["edges"]
            with open(DONE, "a") as fh:
                fh.write(sym + "\n")
            if i % 25 == 0:
                say(f"[{i}/{len(todo)}] {sym}: {edges:,} edges this run")
        except Exception as exc:                       # noqa: BLE001
            fail += 1
            say(f"{sym}: FAIL {type(exc).__name__} {str(exc)[:70]}")
    say(f"DONE ok={ok} fail={fail} no-file={skip} edges={edges:,}")


if __name__ == "__main__":
    main()

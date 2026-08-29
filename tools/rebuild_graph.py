"""Rebuild the knowledge graph from the on-disk sources, from nothing.

WHY THIS EXISTS. The graph is a PROJECTION. Every node and edge in it is
derived from something already on disk - constituent lists, ownership and
fund parquets, 498 validated extractions, 498 harvested RPT filings - so
losing the database costs time and nothing else. That stopped being
hypothetical on 2026-08-30, when a long-lived server process held
~/.shunkan/shunkan.db open across a laptop sleep and the file came back
corrupt AND reverted to its pre-RPT state: 4,399 nodes where there had been
62,220, integrity_check failing across four b-trees, the edge table
unreadable. The sources were untouched.

ORDER MATTERS, AND I GOT IT WRONG THE FIRST TIME. The original comment here
claimed link_legal_names needs the RPT counterparties present, so it ran last.
The dependency is the other way round: the RPT pass resolves filer names like
"Reliance Industries Limited", and unless the legal-name aliases already point
at ticker nodes, those names resolve onto whatever the extraction pass created
- `input:RELIANCE INDUSTRIES LIMITED`, `holder:TATA COMMUNICATIONS LIMITED` -
and the company pages come back empty while the edge count looks right.

link_legal_names needs only the company nodes and BSE's scrip master, both of
which exist after step 1. It runs there.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path


def _stamp(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true",
                    help="required: this replaces the graph database")
    ap.add_argument("--skip-rpt", action="store_true")
    args = ap.parse_args()
    if not args.yes:
        raise SystemExit("refusing to replace the graph without --yes")

    import warnings
    warnings.filterwarnings("ignore")
    from shunkan.config import APP_DIR

    db = APP_DIR / "shunkan.db"
    if db.exists():
        keep = db.with_name(f"shunkan.db.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
        # Move rather than delete. The old file is the only evidence of what
        # went wrong, and a rebuild that turns out worse must be reversible.
        shutil.move(str(db), str(keep))
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.move(str(side), str(keep) + suffix)
        _stamp(f"moved the old database aside -> {keep.name}")

    from shunkan.data import ingest
    from shunkan.data.bse import link_legal_names
    from shunkan.data.llm import _push_graph, load_extraction, stored_symbols
    from shunkan.store.graph import GraphStore

    _stamp("step 1/5  base graph from parquet stores")
    res = ingest.rebuild(verbose=lambda m: _stamp(f"    {m.strip()}"))
    _stamp(f"    {res}")

    _stamp("step 2/5  aliasing legal names onto tickers, BEFORE anything "
           "resolves against them")
    _stamp(f"    {link_legal_names()}")

    _stamp("step 3/5  projecting 498 validated extractions")
    ok = fail = 0
    syms = stored_symbols()
    for i, sym in enumerate(syms, 1):
        ex = load_extraction(sym)
        if ex is None:
            fail += 1
            continue
        try:
            _push_graph(ex)
            ok += 1
        except Exception as exc:                       # noqa: BLE001
            fail += 1
            _stamp(f"    {sym}: FAIL {type(exc).__name__} {str(exc)[:70]}")
        if i % 100 == 0:
            _stamp(f"    [{i}/{len(syms)}]")
    _stamp(f"    extractions pushed ok={ok} fail={fail}")

    if not args.skip_rpt:
        _stamp("step 4/5  related-party filings (this is the slow one)")
        done = APP_DIR / "store" / "bse" / "rpt_ingested.txt"
        if done.exists():
            done.unlink()
        import subprocess
        import sys
        subprocess.run([sys.executable, str(Path(__file__).with_name("reingest_rpt.py"))],
                       check=False)
    else:
        _stamp("step 4/5  SKIPPED")

    # Again at the end: the RPT pass creates counterparty nodes that did not
    # exist during step 2, and some of them are listed companies.
    _stamp("step 5/5  re-aliasing legal names over the new counterparties")
    _stamp(f"    {link_legal_names()}")

    g = GraphStore()
    s = g.stats()
    _stamp(f"REBUILT  {s['nodes']:,} nodes  {s['edges']:,} edges")
    chk = sqlite3.connect(str(db)).execute("PRAGMA integrity_check").fetchone()[0]
    _stamp(f"integrity_check: {chk}")


if __name__ == "__main__":
    main()

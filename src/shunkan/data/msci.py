"""MSCI index review: current constituents and predicted changes.

Index membership is a flow event before it is anything else. An addition
to the MSCI Standard index forces every fund tracking it to buy on one
date, at one price, regardless of what anyone thinks the company is
worth; a deletion does the reverse. Knowing which names are near a
cutoff - and how confidently - is knowing where forced flow will land.

The numbers come from the local msci rule-engine project: its published
constituent lists and its review predictions, each carrying the rule that
decided it. This module imports them into Shunkan's store and joins them
to the equity universe. It computes no prediction of its own; when the
engine has not run for a review, the answer is that it has not run.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from shunkan.data.provider import DataError

DEFAULT_SOURCE = Path.home() / "Projects" / "msci"


def msci_dir(root=None) -> Path:
    from shunkan.store.store import STORE_DIR

    d = (root or STORE_DIR) / "msci"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_pred_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append({
                "symbol": (r.get("symbol") or "").strip().upper(),
                "call": r.get("call"),
                "p_standard": _f(r.get("pStandard")),
                "p_smallcap": _f(r.get("pSmallCap")),
                "p_in_index": _f(r.get("pInIndex")),
                "current_index": r.get("currentIndex"),
                "full_mcap_usd_bn": _f(r.get("fullMcapUsdBn")),
                "x_cutoff": _f(r.get("xStandardOrImiCutoff")),
                "fif": _f(r.get("fif")),
                "atvr_12m_pct": _f(r.get("atvr12mPct")),
                "reason": r.get("decisiveReason"),
            })
    return [r for r in rows if r["symbol"]]


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def import_msci(source: Path | None = None, predictions: Path | None = None,
                root=None) -> dict:
    """Import constituents and the newest prediction file available."""
    src = Path(source or DEFAULT_SOURCE)
    out: dict = {"source": str(src)}
    d = msci_dir(root)

    cons_path = src / "web" / "public" / "data" / "constituents.json"
    if cons_path.exists():
        cons = json.loads(cons_path.read_text())
        rows = []
        for bucket in ("standard", "small"):
            for item in (cons.get(bucket) or []):
                sym = item if isinstance(item, str) else (
                    item.get("symbol") or item.get("ticker") or "")
                if sym:
                    rows.append({"symbol": str(sym).strip().upper(), "index": bucket})
        if rows:
            pd.DataFrame(rows).to_parquet(d / "constituents.parquet", index=False)
            out["constituents"] = len(rows)

    # newest prediction CSV: explicit path, then the project, then Downloads
    cands: list[Path] = []
    if predictions:
        cands.append(Path(predictions))
    for base in (src / "predictions", src, Path.home() / "Downloads"):
        if base.exists():
            cands += sorted(base.glob("msci-*prediction*.csv"))
    pred = next((p for p in reversed(cands)
                 if p.exists() and "changes" not in p.name and "standard" not in p.name),
                None) or next((p for p in reversed(cands) if p.exists()), None)
    if pred is not None:
        rows = _read_pred_csv(pred)
        if rows:
            pd.DataFrame(rows).to_parquet(d / "predictions.parquet", index=False)
            out["predictions"] = len(rows)
            out["prediction_file"] = pred.name
            out["calls"] = {c: sum(1 for r in rows if r["call"] == c)
                            for c in {r["call"] for r in rows if r["call"]}}
    if not out.get("constituents") and not out.get("predictions"):
        # Inside the container the source projects are not mounted; the store
        # is (via ~/.shunkan), so an import run on the HOST is what fills it.
        # Say that instead of failing anonymously.
        raise DataError(
            f"nothing importable under {src} - run the import where the msci "
            "project lives (the store is shared, so the terminal sees it either way)")
    return out


def _load(name: str, root=None) -> pd.DataFrame:
    p = msci_dir(root) / f"{name}.parquet"
    if not p.exists():
        raise DataError(f"MSCI store empty - POST /api/msci/import first ({name})")
    return pd.read_parquet(p)


def review_changes(root=None) -> dict:
    """Names the engine expects to move, ranked by how decisive the call is."""
    df = _load("predictions", root)
    # "hold" is the engine saying nothing happens - it is not a move, and
    # listing 566 of them buries the 100 that are.
    moves = df[df["call"].notna() & (df["call"].str.strip() != "")
               & (~df["call"].str.strip().str.lower().eq("hold"))]
    moves = moves.sort_values("full_mcap_usd_bn", ascending=False)
    return {
        "n_universe": int(len(df)),
        "n_calls": int(len(moves)),
        "rows": [{k: (None if pd.isna(v) else v) for k, v in r.items()}
                 for r in moves.head(60).to_dict("records")],
        "note": ("predictions from the local rule engine, each carrying the "
                 "rule that decided it; MSCI's own announcement is the only "
                 "authority and this is not it"),
    }


def status_for(symbol: str, root=None) -> dict | None:
    """One stock's index standing and pending call, or None if not covered."""
    sym = symbol.upper().replace(".NS", "")
    try:
        df = _load("predictions", root)
    except DataError:
        return None
    row = df[df["symbol"] == sym]
    if row.empty:
        return None
    r = row.iloc[0].to_dict()
    return {k: (None if pd.isna(v) else v) for k, v in r.items()}

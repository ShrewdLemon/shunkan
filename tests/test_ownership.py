"""Ownership structure: SEBI's tree, summed the way SEBI defines it."""

import pandas as pd
import pytest

from shunkan.data.filings import entity_kind, holder_positions


def test_entity_kind_reads_the_legal_suffix():
    # The filing's PAN field is masked, so type comes off the name. These are
    # the forms the promoter tables of NSE companies actually use.
    assert entity_kind("Srichakra Commercials LLP") == "LLP"
    assert entity_kind("Novel Suppliers Pvt Ltd") == "Private company"
    assert entity_kind("Reliance Services and Holdings Limited") == "Company"
    assert entity_kind("SARAOGI FAMILY TRUST (VIVEK SARAOGI-TRUSTEE)") == "Trust"
    assert entity_kind("VIVEK SARAOGI HUF (VIVEK SARAOGI-KARTA)") == "HUF"
    assert entity_kind("Mukesh D Ambani") == "Individual"
    assert entity_kind("SBI MUTUAL FUND") == "Fund"


def test_reverse_lookup_states_its_coverage(tmp_path):
    d = tmp_path / "ownership"
    d.mkdir(parents=True)
    pd.DataFrame([
        {"symbol": "AAA", "as_of": "30-JUN-2026", "holder": "LIC of India",
         "bucket": "inst_domestic", "category": "Insurance", "kind": "Fund",
         "shares": 100, "pct": 5.0, "pledged_pct": None, "beneficial_owner": ""},
        {"symbol": "BBB", "as_of": "30-JUN-2026", "holder": "LIC of India",
         "bucket": "inst_domestic", "category": "Insurance", "kind": "Fund",
         "shares": 200, "pct": 7.5, "pledged_pct": None, "beneficial_owner": ""},
    ]).to_parquet(d / "holders.parquet", index=False)

    out = holder_positions("LIC", root=tmp_path)
    assert len(out["rows"]) == 2
    assert out["companies_scanned"] == 2
    # the honesty that matters: never implies this is the holder's full book
    assert "not the holder's full book" in out["note"]
    assert out["rows"][0]["pct"] == 7.5      # sorted by size


def test_reverse_lookup_is_honest_when_empty(tmp_path):
    out = holder_positions("ANYONE", root=tmp_path)
    assert out["rows"] == [] and out["companies_scanned"] == 0
    assert "no company has been scanned" in out["note"]


@pytest.mark.parametrize("sym", ["RELIANCE"])
def test_live_tree_sums_to_one_hundred(sym):
    """The bug this file exists for: promoter + public must equal 100,
    because SEBI's 'public' already contains the institutions."""
    pytest.importorskip("httpx")
    from shunkan.data.filings import latest_shareholding
    from shunkan.data.provider import DataError

    try:
        sh = latest_shareholding(sym)
    except DataError:
        pytest.skip("NSE unreachable in this environment")
    t = sh.totals
    assert t.get("promoter") and t.get("public")
    assert abs(t["promoter"] + t["public"] - 100.0) < 0.5
    inside = t.get("inst_domestic", 0) + t.get("inst_foreign", 0) + t.get("non_institutions", 0)
    assert inside <= t["public"] + 0.5      # the splits sit INSIDE public
    assert len(sh.holders) > 30             # no arbitrary cap

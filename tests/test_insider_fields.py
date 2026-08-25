"""The NSE PIT feed's field names, pinned to a real record.

Both bugs below shipped and were caught in a screenshot, not by the suite:
a QTY column of zeros and a STAKE column of "0% → 0%". Both looked like data.
"""

from __future__ import annotations

from shunkan.data import filings

# A verbatim record from /api/corporates-pit?symbol=RELIANCE on 2026-08-25,
# trimmed to the fields that matter. Note buyQuantity and sellquantity are
# '0' - they are '0' in 20 of 20 rows - while secAcq carries the real size.
RAW = {
    "acqName": "BALANADU NARAYAN", "personCategory": "Other",
    "acqMode": "Off Market", "tdpTransactionType": "Sell",
    "secType": "Equity Shares", "date": "18-Feb-2026 19:06",
    "secAcq": "2320", "buyQuantity": "0", "sellquantity": "0",
    "secVal": "3294168",
    "befAcqSharesNo": "3920", "afterAcqSharesNo": "1600",
    "befAcqSharesPer": "0", "afterAcqSharesPer": "0",
    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/IT_1194033_1",
}

# The promoter-group row, where the holder had nothing before: NSE writes the
# string 'Nil', which is a zero holding and not a missing field.
RAW_NIL = {**RAW, "acqName": "Reliance Industrial Investments and Holdings Limited",
           "personCategory": "Promoter Group", "tdpTransactionType": "Buy",
           "secAcq": "240942006", "befAcqSharesNo": "Nil",
           "afterAcqSharesNo": "240942006",
           "befAcqSharesPer": "0", "afterAcqSharesPer": "3.56"}


def _one(monkeypatch, raw):
    monkeypatch.setattr(filings, "_rows", lambda *a, **k: [raw])
    return filings.insider_trades("RELIANCE")[0]


def test_quantity_comes_from_sec_acq_not_the_empty_buy_sell_pair(monkeypatch):
    """Reading buyQuantity/sellquantity rendered a column of zeros."""
    assert _one(monkeypatch, RAW)["qty"] == 2320


def test_quantity_reconciles_with_the_share_counts(monkeypatch):
    """3920 - 2320 = 1600, which is how we know secAcq is the trade size."""
    r = _one(monkeypatch, RAW)
    assert r["shares_before"] - r["qty"] == r["shares_after"]


def test_a_rounded_to_zero_percentage_is_not_reported_as_zero(monkeypatch):
    """The person holds 3,920 shares. The feed rounds that to 0% against a
    676-crore share count. Printing '0% → 0%' asserts they hold nothing."""
    r = _one(monkeypatch, RAW)
    assert r["pct_before"] is None
    assert r["pct_after"] is None
    # the fact the filing DOES assert survives
    assert (r["shares_before"], r["shares_after"]) == (3920, 1600)


def test_a_real_percentage_is_kept(monkeypatch):
    assert _one(monkeypatch, RAW_NIL)["pct_after"] == 3.56


def test_nil_holding_is_zero_not_missing(monkeypatch):
    """'Nil' means the holder had none, which is a fact. _num would have
    returned None and the UI could not tell that from an absent field."""
    r = _one(monkeypatch, RAW_NIL)
    assert r["shares_before"] == 0
    assert r["shares_after"] == 240942006


def test_value_is_carried_through(monkeypatch):
    assert _one(monkeypatch, RAW)["value"] == 3294168.0


def test_person_category_is_preserved(monkeypatch):
    """The category IS the signal - a promoter buying is not an ESOP exercise."""
    assert _one(monkeypatch, RAW_NIL)["category"] == "Promoter Group"

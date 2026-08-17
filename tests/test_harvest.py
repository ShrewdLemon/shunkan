"""Harvesting option history before it becomes unbuyable.

Kite deletes an expired contract from the instruments master, so its candles
and even its token are gone after expiry. A contract still listed carries its
whole traded life. That asymmetry makes this the only job here with a real
deadline, and these tests pin the properties that make it safe to run daily.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from shunkan.data.harvest import _upsert, contracts_dir, coverage


def _rows(symbol="NIFTY2681824400CE", ts=("2026-08-14", "2026-08-17"), close=100.0):
    return [{"ts": t, "tradingsymbol": symbol, "expiry": "2026-08-18",
             "strike": 24400.0, "right": "CE", "lot_size": 65,
             "open": close, "high": close, "low": close, "close": close,
             "volume": 1000, "oi": 5000} for t in ts]


def test_upsert_is_idempotent(tmp_path):
    """A daily job you are afraid to re-run does not get run. Re-running must
    never duplicate a (tradingsymbol, ts) row."""
    path = tmp_path / "NIFTY_2026-08-18_day.parquet"
    assert _upsert(path, _rows()) == 2
    assert _upsert(path, _rows()) == 2
    assert _upsert(path, _rows()) == 2


def test_reharvest_takes_the_newer_value(tmp_path):
    """Kite revises a candle intraday. Last write wins, so a re-run late in
    the session corrects a partial bar rather than keeping the stale one."""
    path = tmp_path / "NIFTY_2026-08-18_day.parquet"
    _upsert(path, _rows(close=100.0))
    _upsert(path, _rows(close=137.5))
    df = pd.read_parquet(path)
    assert len(df) == 2
    assert set(df["close"]) == {137.5}


def test_different_contracts_coexist(tmp_path):
    path = tmp_path / "NIFTY_2026-08-18_day.parquet"
    _upsert(path, _rows("NIFTY2681824400CE"))
    _upsert(path, _rows("NIFTY2681824400PE"))
    assert len(pd.read_parquet(path)) == 4


def test_coverage_reports_what_is_actually_held(tmp_path):
    """A gap has to be visible. Assuming an expiry is absent because nobody
    looked is how a week goes missing."""
    (tmp_path / "contracts").mkdir(exist_ok=True)
    _upsert(tmp_path / "contracts" / "NIFTY_2026-08-18_day.parquet", _rows())
    c = coverage(root=tmp_path)
    assert list(c["symbol"]) == ["NIFTY"]
    assert int(c["candles"].iloc[0]) == 2
    assert int(c["contracts"].iloc[0]) == 1
    assert c["first"].iloc[0] == date(2026, 8, 14)


def test_a_corrupt_file_does_not_lose_todays_pull(tmp_path):
    """The irreplaceable thing is the data in hand. If the existing file
    cannot be read, rewrite it rather than aborting and losing the fetch."""
    path = tmp_path / "NIFTY_2026-08-18_day.parquet"
    path.write_bytes(b"not a parquet file")
    assert _upsert(path, _rows()) == 2

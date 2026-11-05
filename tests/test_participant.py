"""NSE participant-wise positioning: parsing and the change computation."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest

from shunkan.data import participant
from shunkan.data.participant import (
    backfill,
    change_between,
    day_table,
    fetch_participant_oi,
    latest_with_change,
    parse_participant_csv,
    store_path,
)
from shunkan.data.provider import DataError, FetchFailed, Unpublished

FIXTURE = Path(__file__).parent / "fixtures" / "fao_participant_oi_03112026.csv"
NOV3, NOV4 = date(2026, 11, 3), date(2026, 11, 4)
DENIED = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission"

# Trimmed from a real archive file: quoted title line, trailing-space headers,
# TOTAL row at the bottom.
SAMPLE = '''""Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Aug 14, 2026"",,,,,,,,,,,,,,
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short       ,Option Index Call Long,Option Index Put Long,Option Index Call Short,Option Index Put Short,Option Stock Call Long,Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,Total Long Contracts      ,Total Short Contracts
Client,211638,55956,3275125,261974,3205535,2649873,3072956,3257565,2832221,977719,1540281,1301705,13152111,9490438
DII,50830,20040,329392,4402412,7380,50603,130,0,928,40785,352194,19703,479918,4794479
FII,88749,126144,2049008,772525,564363,510844,371673,476243,491199,317727,600615,352530,4021890,2699730
Pro,71028,220105,1146046,1362680,1234567,1000000,1500000,900000,500000,300000,400000,250000,4251641,4632785
TOTAL,422245,422245,6799571,6799591,5011845,4211320,4944759,4633808,3824348,1636231,2893090,1923938,21905530,21617432
'''


def test_parse_handles_nse_quirks():
    df = parse_participant_csv(SAMPLE, date(2026, 8, 14))
    assert list(df["client_type"]) == ["Client", "DII", "FII", "Pro"]  # TOTAL dropped
    assert df.loc[df.client_type == "FII", "fut_idx_long"].iloc[0] == 88749
    # trailing-space header column parsed
    assert df.loc[df.client_type == "DII", "fut_stk_short"].iloc[0] == 4402412


def test_nets_follow_the_stated_direction_convention():
    """Long calls + short puts = bullish; short calls + long puts = bearish."""
    df = parse_participant_csv(SAMPLE, date(2026, 8, 14)).set_index("client_type")
    fii = df.loc["FII"]
    assert fii.idx_fut_net == 88749 - 126144
    assert fii.idx_opt_net == (564363 + 476243) - (371673 + 510844)


def test_a_changed_format_refuses_rather_than_misparses():
    with pytest.raises(DataError, match="header"):
        parse_participant_csv("some,other,file\n1,2,3", date(2026, 8, 14))


def test_change_needs_two_days_and_never_invents_one(tmp_path):
    assert latest_with_change(root=tmp_path) is None       # nothing on disk
    one = parse_participant_csv(SAMPLE, date(2026, 8, 13))
    one.to_parquet(store_path(tmp_path), index=False)
    assert latest_with_change(root=tmp_path) is None       # one day is not a change


def test_change_reads_the_day_over_day_move(tmp_path):
    d1 = parse_participant_csv(SAMPLE, date(2026, 8, 13))
    d2 = parse_participant_csv(SAMPLE, date(2026, 8, 14))
    # make FII add 1,000 net long futures on day 2
    d2.loc[d2.client_type == "FII", "fut_idx_long"] += 1000
    d2["idx_fut_net"] = d2.fut_idx_long - d2.fut_idx_short
    pd.concat([d1, d2]).to_parquet(store_path(tmp_path), index=False)

    out = latest_with_change(root=tmp_path)
    fii = out["by_participant"]["FII"]
    assert fii["idx_fut_net_chg"] == 1000
    assert fii["read"] == "added bullish exposure"


def _serve(files: dict, calls: list):
    """MockTransport client serving {DDMMYYYY: bytes}; anything else 404s."""
    def handler(request):
        calls.append(str(request.url))
        for key, body in files.items():
            if request.url.path.endswith(f"fao_participant_oi_{key}.csv"):
                return httpx.Response(200, content=body)
        return httpx.Response(404, content=b"Not Found")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_reads_the_real_archive_file():
    calls = []
    df = fetch_participant_oi(NOV3, client=_serve({"03112026": FIXTURE.read_bytes()}, calls))
    assert calls == ["https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_03112026.csv"]
    assert list(df["client_type"]) == ["Client", "DII", "FII", "Pro"]
    assert df.loc[df.client_type == "FII", "fut_idx_long"].iloc[0] == 33148


def test_fetch_splits_unpublished_from_access_denied():
    with pytest.raises(Unpublished):
        fetch_participant_oi(NOV3, client=_serve({}, []))
    with pytest.raises(FetchFailed):
        fetch_participant_oi(NOV3, client=_serve({"03112026": DENIED}, []))


@pytest.mark.parametrize("body", [b"title\nClient Type,Foo\nFII,1\n",
                                  b"Client Type,a\n\"unclosed,1,2,3\n1,2\n"])
def test_a_200_in_a_new_format_is_a_failed_fetch_and_not_cached(tmp_path, body):
    with pytest.raises(FetchFailed, match="fao_participant_oi_03112026"):
        fetch_participant_oi(NOV3, client=_serve({"03112026": body}, []))
    with pytest.raises(FetchFailed):
        day_table(NOV3, root=tmp_path, client=_serve({"03112026": body}, []))
    assert not list(tmp_path.rglob("*.csv"))

def test_backfill_counts_404_as_unpublished_and_html_as_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(participant.time, "sleep", lambda s: None)
    today, weekdays, days = date.today(), 0, 0
    while weekdays < 2:
        weekdays += (today - timedelta(days=days)).weekday() < 5
        days += 1
    replies = iter([httpx.Response(404, content=b"Not Found"), httpx.Response(200, content=DENIED)])
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(replies)))

    out = backfill(days, root=tmp_path, client=client)
    assert (out["fetched"], out["unpublished"], out["failed"]) == (0, 1, 1)


def test_day_table_caches_the_raw_file_and_reads_it_offline_after(tmp_path):
    calls = []
    client = _serve({"03112026": FIXTURE.read_bytes()}, calls)
    first = day_table(NOV3, root=tmp_path, client=client)
    raw = tmp_path / "participant" / "raw" / "2026" / "fao_participant_oi_03112026.csv"
    assert raw.read_bytes() == FIXTURE.read_bytes()
    second = day_table(NOV3, root=tmp_path, client=client)
    assert len(calls) == 1
    pd.testing.assert_frame_equal(first, second)
    assert first["date"].iloc[0] == pd.Timestamp(NOV3)


def test_day_table_caches_nothing_it_could_not_fetch(tmp_path):
    with pytest.raises(FetchFailed):
        day_table(NOV3, root=tmp_path, client=_serve({"03112026": DENIED}, []))
    assert not list(tmp_path.rglob("*.csv"))


def test_change_between_two_archive_days(tmp_path):
    nov3 = FIXTURE.read_bytes()
    assert nov3.count(b"\nFII,33148,") == 1
    nov4 = nov3.replace(b"\nFII,33148,", b"\nFII,34148,")
    calls = []
    out = change_between(NOV4, NOV3, root=tmp_path,
                         client=_serve({"03112026": nov3, "04112026": nov4}, calls))
    assert len(calls) == 2
    assert out["date"] == "2026-11-04" and out["prev_date"] == "2026-11-03"
    fii = out["by_participant"]["FII"]
    assert fii["idx_fut_net_chg"] == 1000
    assert fii["idx_opt_net_chg"] == 0
    assert fii["read"] == "added bullish exposure"
    assert out["by_participant"]["DII"]["read"] == "unchanged"
    assert set(out["by_participant"]) == {"Client", "DII", "FII", "Pro"}


def test_change_between_matches_latest_with_change(tmp_path):
    nov3 = FIXTURE.read_bytes()
    nov4 = nov3.replace(b"\nFII,33148,", b"\nFII,34148,")
    client = _serve({"03112026": nov3, "04112026": nov4}, [])
    pd.concat([day_table(NOV3, root=tmp_path, client=client),
               day_table(NOV4, root=tmp_path, client=client)]).to_parquet(
        store_path(tmp_path), index=False)
    assert change_between(NOV4, NOV3, root=tmp_path, client=client) == latest_with_change(root=tmp_path)

"""NSE participant-wise positioning: parsing and the change computation."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from shunkan.data.participant import (
    latest_with_change,
    parse_participant_csv,
    store_path,
)
from shunkan.data.provider import DataError

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

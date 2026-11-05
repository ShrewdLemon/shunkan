"""End-of-day OI and straddle views from NSE F&O bhavcopies."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics import oi_eod
from shunkan.analytics.daily import _max_pain
from shunkan.data.provider import DataError

D = date(2026, 11, 5)
EXP = pd.Timestamp("2026-11-09")
NEXT = pd.Timestamp("2026-11-17")


def frame(day, rows, spot=24536.6, lot=65.0, symbol="NIFTY", kind="OPTIDX"):
    """rows: (expiry, strike, right, oi, oi_change, close, settle, contracts)."""
    out = [dict(date=pd.Timestamp(day), kind=kind, symbol=symbol, expiry=pd.Timestamp(e), strike=float(k), right=r,
                open=c, high=c, low=c, close=c, settle=s, contracts=n, oi=float(oi), oi_change=float(ch),
                underlying=spot, lot=lot)
           for e, k, r, oi, ch, c, s, n in rows]
    return pd.DataFrame(out)


LADDER = [  # strike, CE oi, CE chg, PE oi, PE chg
    (24300, 650_000, -65_000, 6_500_000, 6_370_000),
    (24400, 1_300_000, 130_000, 3_250_000, 3_250_000),
    (24500, 5_200_000, -3_250_000, 5_850_000, 5_525_000),
    (24550, 4_290_000, 2_678_000, 2_990_000, 2_990_000),
    (24600, 6_500_000, 2_340_000, 1_950_000, 0),
    (25000, 11_960_000, 91_000, 65_000, 0),
]


def ladder(day=D, expiry=EXP, spot=24536.6, untraded=()):
    rows = []
    for k, coi, cch, poi, pch in LADDER:
        n = 0 if k in untraded else 10
        rows += [(expiry, k, "CE", coi, cch, 100.0, 99.0, n), (expiry, k, "PE", poi, pch, 80.0, 81.0, n)]
    return frame(day, rows, spot=spot)


def test_by_strike_reads_contracts_change_walls_and_max_pain():
    df = ladder(untraded=(25000,))
    out = oi_eod.by_strike(df, EXP)
    assert out["unit"] == "contracts" and out["lot"] == 65.0 and out["spot"] == 24536.6
    assert [r["strike"] for r in out["rows"]] == [24300, 24400, 24500, 24550, 24600, 25000]
    r = out["rows"][2]
    assert r["call_oi"] == 80_000 and r["call_oi_change"] == -50_000
    assert r["put_oi"] == 90_000 and r["put_oi_change"] == 85_000
    top = out["rows"][-1]
    assert top["call_price"] == 99.0 and top["call_traded"] is False and out["rows"][0]["call_price"] == 100.0
    assert out["call_wall"] == 25000 and out["put_wall"] == 24300
    assert out["atm_strike"] == 24550
    ks = np.array([k for k, *_ in LADDER], float)
    want = _max_pain(ks, np.array([c for _, c, *_ in LADDER], float), np.array([p for *_, p, _ in LADDER], float))
    assert out["max_pain"] == want
    ce = sum(c for _, c, *_ in LADDER); pe = sum(p for *_, p, _ in LADDER)
    assert out["pcr"] == pytest.approx(pe / ce)
    assert out["totals"] == {"call_oi": ce / 65, "put_oi": pe / 65,
                             "call_oi_change": sum(c for _, _, c, _, _ in LADDER) / 65,
                             "put_oi_change": sum(c for *_, c in LADDER) / 65}


def test_by_strike_keeps_width_strikes_each_side_of_the_atm():
    rows = [(EXP, 24000 + 50 * i, r, 1000, 0, 10.0, 10.0, 1) for i in range(41) for r in ("CE", "PE")]
    out = oi_eod.by_strike(frame(D, rows, spot=25010.0), EXP, width=3)
    assert [r["strike"] for r in out["rows"]] == [24850, 24900, 24950, 25000, 25050, 25100, 25150]


def test_by_strike_without_a_lot_reports_units():
    out = oi_eod.by_strike(frame(D, [(EXP, 24500, "CE", 650, 65, 1.0, 1.0, 1), (EXP, 24500, "PE", 130, 0, 1.0, 1.0, 1)],
                                 lot=np.nan), EXP)
    assert out["unit"] == "units" and out["lot"] is None and out["rows"][0]["call_oi"] == 650


def test_by_strike_refuses_a_missing_expiry():
    with pytest.raises(DataError, match="no NIFTY contracts expiring 2026-11-17"):
        oi_eod.by_strike(ladder(), NEXT)


def test_strike_keys_match_javascripts_number_spelling():
    assert [oi_eod.strike_key(k) for k in (24300.0, 24300, 1312.5, 57000.0)] == ["24300", "24300", "1312.5", "57000"]


def test_strikes_are_two_below_and_three_above_on_the_double_grid():
    ks = [24000 + 50 * i for i in range(30)]
    assert oi_eod.pick_strikes(ks, 24536.6) == [24300, 24400, 24500, 24600, 24700, 24800]
    assert oi_eod.pick_strikes([24400, 24500, 24600], 24536.6) == [24400, 24500, 24600]


def test_series_history_starts_where_the_strikes_carry_oi():
    frames = []
    for i, d in enumerate(pd.bdate_range("2026-10-26", "2026-11-05")):
        oi = [0, 0, 0, 1_000, 30_000, 120_000, 300_000, 650_000, 1_300_000][i]
        frames.append((d.date(), frame(d, [(EXP, 24500, "CE", oi, 0, 1.0, 1.0, 1), (EXP, 24500, "PE", oi / 2, 0, 1.0, 1.0, 1)],
                                       spot=24000 + i)))
    frames.append((date(2026, 11, 6), frame("2026-11-06", [(NEXT, 24500, "CE", 1, 0, 1.0, 1.0, 1)])))
    out = oi_eod.series_oi(frames, EXP, [24500])
    assert out["listed"] is None and out["listed_before"] == "2026-10-26"
    assert out["days"] == ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    assert out["oi"]["24500"]["call"] == [v / 65 for v in (30_000, 120_000, 300_000, 650_000, 1_300_000)]
    assert out["oi"]["24500"]["put"] == [v / 130 for v in (30_000, 120_000, 300_000, 650_000, 1_300_000)]
    assert out["spot"] == [24004, 24005, 24006, 24007, 24008] and out["unit"] == "contracts"


def test_straddle_takes_the_nearest_expiry_after_the_day_and_the_atm_strike():
    def day(d, spot, front):
        rows = []
        for e, base in ((front, 100.0), (NEXT, 300.0)):
            for k in (24500, 24550, 24600):
                rows += [(e, k, "CE", 1, 0, base + 24550 - k, base, 5), (e, k, "PE", 1, 0, base - 24550 + k, base, 5)]
        return frame(d, rows, spot=spot)

    nov5 = day("2026-11-05", 24536.6, EXP)
    expiry_day = day("2026-11-09", 24575.0, EXP)
    rows = oi_eod.straddles([(date(2026, 11, 5), nov5), (date(2026, 11, 9), expiry_day)],
                            holidays=[date(2026, 11, 8), date(2026, 11, 10)])
    a, b = rows
    assert (a["strike"], a["expiry"], a["straddle"], a["sessions_to_expiry"]) == (24550, "2026-11-09", 200.0, 2)
    assert a["implied_move_pct"] == pytest.approx(200 / 24536.6 * 100)
    assert (b["strike"], b["expiry"], b["straddle"], b["sessions_to_expiry"]) == (24550, "2026-11-17", 600.0, 5)
    assert a["traded"] is True


def test_straddle_ties_go_to_the_lower_strike_and_untraded_legs_use_settle():
    rows = [(EXP, 24500, "CE", 1, 0, 120.0, 110.0, 0), (EXP, 24500, "PE", 1, 0, 90.0, 95.0, 3),
            (EXP, 24600, "CE", 1, 0, 70.0, 70.0, 3), (EXP, 24600, "PE", 1, 0, 140.0, 140.0, 3)]
    (r,) = oi_eod.straddles([(D, frame(D, rows, spot=24550.0))])
    assert r["strike"] == 24500 and r["straddle"] == 110.0 + 90.0 and r["traded"] is False


def test_view_lists_expiries_guards_each_section_and_falls_back_to_the_front(monkeypatch):
    days = [d.date() for d in pd.bdate_range("2026-10-26", "2026-11-05")]
    monkeypatch.setattr(oi_eod.fo_bhav, "sessions", lambda root=None: days)
    monkeypatch.setattr(oi_eod, "_options", lambda d, sym, root: ladder(day=d))
    monkeypatch.setattr(oi_eod, "_holidays", lambda root: [])
    out = oi_eod.view("nifty", expiry="2030-01-01")
    assert out["symbol"] == "NIFTY" and out["as_of"] == "2026-11-05" and out["expiry"] == "2026-11-09"
    assert out["expiries"] == ["2026-11-09"] and out["source"] == oi_eod.SOURCE
    assert out["by_strike"]["call_wall"] == 25000
    assert out["history"]["strikes"] == [24300, 24400, 24500, 24550, 24600, 25000]
    assert len(out["straddle"]["rows"]) == len(days)


def test_view_reports_a_section_error_without_failing_the_others(monkeypatch):
    monkeypatch.setattr(oi_eod.fo_bhav, "sessions", lambda root=None: [D])
    monkeypatch.setattr(oi_eod, "_options", lambda d, sym, root: ladder(day=d))
    monkeypatch.setattr(oi_eod, "_holidays", lambda root: [])
    monkeypatch.setattr(oi_eod, "series_oi", lambda *a, **k: (_ for _ in ()).throw(DataError("series broke")))
    out = oi_eod.view("NIFTY")
    assert out["history"] == {"error": "series broke"}
    assert "rows" in out["by_strike"] and "rows" in out["straddle"]


def test_view_without_bhavcopies_or_options_is_an_error(monkeypatch):
    monkeypatch.setattr(oi_eod.fo_bhav, "sessions", lambda root=None: [])
    assert oi_eod.view("NIFTY")["error"] == "no F&O bhavcopy on disk"
    monkeypatch.setattr(oi_eod.fo_bhav, "sessions", lambda root=None: [D])
    monkeypatch.setattr(oi_eod, "_options", lambda d, sym, root: ladder().iloc[0:0])
    assert oi_eod.view("ZZZ")["error"] == "no ZZZ options in the 2026-11-05 bhavcopy"


def test_one_sided_strikes_count_toward_walls_totals_and_pcr():
    df = pd.concat([ladder(), frame(D, [(EXP, 25500, "CE", 13_000_000, 650_000, 2.0, 2.0, 4)])], ignore_index=True)
    out = oi_eod.by_strike(df, EXP)
    assert out["call_wall"] == 25500
    ce = sum(c for _, c, *_ in LADDER) + 13_000_000
    assert out["totals"]["call_oi"] == ce / 65 and out["pcr"] == pytest.approx(sum(p for *_, p, _ in LADDER) / ce)
    row = next(r for r in out["rows"] if r["strike"] == 25500)
    assert row["put_oi"] == 0 and row["put_price"] is None and row["put_traded"] is False and row["call_oi"] == 200_000


def test_a_series_is_counted_in_its_latest_lot_so_a_lot_change_does_not_jump():
    frames = [(date(2026, 10, 27), frame("2026-10-27", [(EXP, 500, "CE", 1_000_000, 0, 1.0, 1.0, 1),
                                                        (EXP, 500, "PE", 0, 0, 1.0, 1.0, 1)], spot=500.0, lot=1000.0)),
              (date(2026, 10, 28), frame("2026-10-28", [(EXP, 500, "CE", 1_020_000, 0, 1.0, 1.0, 1),
                                                        (EXP, 500, "PE", 0, 0, 1.0, 1.0, 1)], spot=500.0, lot=500.0))]
    out = oi_eod.series_oi(frames, EXP, [500])
    assert out["oi"]["500"]["call"] == [2000, 2040] and out["lot"] == 500.0


def test_listed_is_only_claimed_when_the_window_shows_the_listing():
    seen = [(d.date(), frame(d, [(EXP, 500, "CE", 100, 0, 1.0, 1.0, 1), (EXP, 500, "PE", 100, 0, 1.0, 1.0, 1)], spot=500.0))
            for d in pd.bdate_range("2026-10-26", "2026-10-28")]
    out = oi_eod.series_oi(seen, EXP, [500])
    assert out["listed"] is None and out["listed_before"] == "2026-10-26"
    before = [(date(2026, 10, 23), frame("2026-10-23", [(NEXT, 500, "CE", 1, 0, 1.0, 1.0, 1)], spot=500.0))]
    out = oi_eod.series_oi(before + seen, EXP, [500])
    assert out["listed"] == "2026-10-26" and out["listed_before"] is None

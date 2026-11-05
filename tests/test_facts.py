"""The facts card on a temp store: settled bars, positioning, compose, refresh."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from shunkan.analytics import facts
from shunkan.data import constituents, flows, fo_bhav, index_bars, participant
from shunkan.data import events_calendar as cal
from shunkan.data.provider import DataError
from shunkan.markets import IST

FIX = Path(__file__).parent / "fixtures"
HOLIDAYS = json.loads((FIX / "holidays.json").read_text())
BOARD = json.loads((FIX / "board_meetings.json").read_text())
FIIDII = json.loads((FIX / "fiidii.json").read_text())
XLS = (FIX / "fii_stats_03-Nov-2026.xls").read_bytes()
PART = (FIX / "fao_participant_oi_03112026.csv").read_text()
DENIED = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission"

NOV2, NOV3, NOV4, NOV6 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 6)
EVENING = datetime(2026, 11, 3, 20, 0, tzinfo=IST)
FRONT, NEXT, GONE, MONTH = "2026-11-09", "2026-11-17", "2026-11-03", "2026-11-23"
WARNING = ("expiry day: OI walls expire at 15:30 and overnight OI is mostly closing "
           "positions; read intraday migration, not these levels.")

CHAIN = {
    23800: (750_000, 3_000_000, 260, 25, -75_000, 375_000),
    23900: (1_500_000, 4_500_000, 180, 45, 0, 1_500_000),
    24000: (3_750_000, 3_750_000, 110, 75, 750_000, 225_000),
    24100: (6_000_000, 1_500_000, 60, 125, 2_250_000, -150_000),
    24200: (4_500_000, 750_000, 30, 195, 1_125_000, 75_000),
}

UDIFF_COLS = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
              "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
              "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty",
              "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]


def contracts(expired=True):
    rows = []
    for k, (coi, poi, cs, ps, cc, pc) in CHAIN.items():
        rows += [("IDO", "NIFTY", FRONT, k, "CE", cs, coi, cc),
                 ("IDO", "NIFTY", FRONT, k, "PE", ps, poi, pc)]
    rows += [("IDO", "NIFTY", NEXT, 24000, "CE", 150, 1_500_000, 0),
             ("IDO", "NIFTY", NEXT, 24000, "PE", 120, 1_200_000, 0),
             ("IDF", "NIFTY", MONTH, None, None, 24110, 12_000_000, 0),
             ("IDO", "BANKNIFTY", MONTH, 52000, "CE", 400, 315_000, 0),
             ("IDO", "BANKNIFTY", MONTH, 52000, "PE", 380, 420_000, 0),
             ("IDF", "BANKNIFTY", MONTH, None, None, 52100, 2_100_000, 0),
             ("STO", "RELIANCE", MONTH, 1400, "CE", 20, 500_000, 0)]
    if expired:
        rows += [("IDO", "NIFTY", GONE, 24000, "CE", 30, 9_000_000, 9_000_000),
                 ("IDO", "NIFTY", GONE, 24000, "PE", 0.05, 300_000, 0)]
    return rows


def bhav_bytes(day, rows):
    lines = [",".join(UDIFF_COLS)]
    for tp, sym, exp, strike, right, px, oi, chg in rows:
        v = dict.fromkeys(UDIFF_COLS, "")
        v.update(TradDt=day.isoformat(), BizDt=day.isoformat(), Sgmt="FO", Src="NSE",
                 FinInstrmTp=tp, TckrSymb=sym, XpryDt=exp, FininstrmActlXpryDt=exp,
                 StrkPric="" if strike is None else f"{strike:.2f}", OptnTp=right or "",
                 OpnPric=str(px), HghPric=str(px), LwPric=str(px),
                 ClsPric=str(px if tp == "IDF" else px + 7),
                 SttlmPric=str(px + 7 if tp == "IDF" else px),
                 OpnIntrst=str(oi), ChngInOpnIntrst=str(chg),
                 TtlTradgVol="10", SsnId="F1",
                 UndrlygPric={"NIFTY": "24025", "BANKNIFTY": "52010"}.get(sym, "1400"),
                 NewBrdLotQty={"NIFTY": "75", "BANKNIFTY": "35"}.get(sym, "500"))
        lines.append(",".join(v[k] for k in UDIFF_COLS))
    name = f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


def write_bhav(root, day, rows):
    p = fo_bhav.raw_dir(root) / f"{day:%Y}" / f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bhav_bytes(day, rows))
    return p


def history(last=NOV3, extra=None):
    days = pd.bdate_range("2026-09-21", last)
    closes = 23500 + 10 * np.arange(len(days)) + 40 * np.sin(np.arange(len(days)))
    df = pd.DataFrame({"date": days, "open": closes - 5, "high": closes + 30,
                       "low": closes - 30, "close": closes, "volume": 1000,
                       "source": "yahoo",
                       "captured_at": [f"{d:%Y-%m-%d}T10:30:00+00:00" for d in days]})
    if last == NOV3:
        df.loc[df.index[-2], ["open", "high", "low", "close"]] = [23880, 23960, 23850, 23900]
        df.loc[df.index[-1], ["open", "high", "low", "close"]] = [23950, 24080, 23920, 24030]
    if extra is not None:
        df = pd.concat([df, pd.DataFrame([extra])], ignore_index=True)
    return df


def write_history(root, df, name="_NSEI"):
    p = Path(root) / "history" / f"{name}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return p


def write_vix(root):
    df = pd.DataFrame({"date": pd.to_datetime(["2008-01-02", "2015-06-01", "2020-03-23",
                                               "2026-11-02", "2026-11-03"]),
                       "close": [20.0, 15.0, 30.0, 12.0, 14.0]})
    write_history(root, df, "_INDIAVIX")


class FakeWWW:
    def __init__(self, routes=None):
        self.routes = routes or {cal.HOLIDAYS_PATH: HOLIDAYS, cal.BOARD_PATH: BOARD,
                                 flows.FIIDII_PATH: FIIDII}
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append(path)
        out = self.routes[path]
        if isinstance(out, Exception):
            raise out
        return out


class NoWWW:
    def get_json(self, path, params=None):
        raise AssertionError(f"compose touched www: {path}")


def members(monkeypatch):
    c = constituents.Constituent
    lists = {"NIFTY50": [c("BHARTIARTL", "Bharti Airtel", ("NIFTY50",)),
                         c("HINDALCO", "Hindalco Industries", ("NIFTY50",)),
                         c("ONGC", "Oil and Natural Gas Corporation", ("NIFTY50",))],
             "BANKNIFTY": [c("SBIN", "State Bank of India", ("BANKNIFTY",))]}
    monkeypatch.setattr(constituents, "fetch_constituents", lambda name: lists[name])


@pytest.fixture
def net(monkeypatch):
    seen = []

    def boom(*a, **k):
        seen.append(a)
        raise AssertionError("network call")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr(constituents, "_mem", {})
    return seen


def store(root, monkeypatch, participant_files=True):
    write_bhav(root, NOV3, contracts())
    write_history(root, history())
    write_vix(root)
    if participant_files:
        p3 = participant.raw_path(NOV3, root)
        p3.parent.mkdir(parents=True, exist_ok=True)
        p3.write_text(PART)
        participant.raw_path(NOV2, root).write_text(PART.replace("FII,33148,", "FII,32148,"))
    fs = flows.fii_stats_path(NOV3, root)
    fs.parent.mkdir(parents=True, exist_ok=True)
    fs.write_bytes(XLS)
    fd = flows.fiidii_path(NOV3, root)
    fd.parent.mkdir(parents=True, exist_ok=True)
    fd.write_text(json.dumps(FIIDII))
    warm = EVENING - timedelta(hours=1)
    cal.holidays(root=root, www=FakeWWW(), now=warm)
    cal.board_meetings(NOV3, date(2026, 11, 5), root=root, www=FakeWWW(), now=warm)
    members(monkeypatch)
    cal.index_universe(root=root, now=warm)


SECTIONS = {"session", "price", "positioning", "flows", "expiry", "calendar"}


def frame(*rows):
    return pd.DataFrame([{"date": pd.Timestamp(d), "close": c, "captured_at": cap}
                         for d, c, cap in rows])


def test_settled_bars_drop_a_bar_captured_before_the_close():
    df = frame((NOV3, 1.0, "2026-11-03T06:30:00+00:00"), (NOV4, 2.0, "2026-11-04T06:30:00+00:00"))
    out, dropped = facts.settled_bars(df, datetime(2026, 11, 4, 16, 0, tzinfo=IST))
    assert list(out["close"]) == [1.0]
    assert dropped["date"] == "2026-11-04" and "12:00 IST" in dropped["reason"]


def test_settled_bars_keep_a_bar_captured_after_the_close():
    df = frame((NOV3, 1.0, "2026-11-03T06:30:00+00:00"), (NOV4, 2.0, "2026-11-04T10:15:00+00:00"))
    out, dropped = facts.settled_bars(df, datetime(2026, 11, 4, 16, 0, tzinfo=IST))
    assert list(out["close"]) == [1.0, 2.0] and dropped is None


def test_settled_bars_always_keep_yesterday():
    df = frame((NOV3, 1.0, "2026-11-03T05:00:00+00:00"))
    out, dropped = facts.settled_bars(df, datetime(2026, 11, 4, 9, 0, tzinfo=IST))
    assert list(out["close"]) == [1.0] and dropped is None


def test_settled_bars_without_capture_time_use_the_clock():
    df = pd.DataFrame({"date": pd.to_datetime([NOV3, NOV4]), "close": [1.0, 2.0]})
    out, dropped = facts.settled_bars(df, datetime(2026, 11, 4, 15, 0, tzinfo=IST))
    assert list(out["close"]) == [1.0] and dropped["date"] == "2026-11-04"
    out, dropped = facts.settled_bars(df, datetime(2026, 11, 4, 15, 30, tzinfo=IST))
    assert list(out["close"]) == [1.0, 2.0] and dropped is None


def test_eod_positioning_hand_computed(tmp_path):
    write_bhav(tmp_path, NOV3, contracts())
    p = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path, spot=24030.0)
    assert p["as_of"] == "2026-11-03" and p["source"] == "NSE F&O bhavcopy 2026-11-03 (end of day)"
    assert p["front_expiry"] == FRONT and p["next_expiry"] == NEXT and p["lot"] == 75
    assert p["pcr_front"] == pytest.approx(13.5 / 16.5) and p["pcr_next"] == pytest.approx(0.8)
    assert (p["max_pain"], p["call_wall"], p["put_wall"]) == (24000, 24100, 23900)
    assert [(a["strike"], a["oi_change"], a["lots"]) for a in p["call_oi_adds"]] == [
        (24100, 2_250_000, 30000), (24200, 1_125_000, 15000), (24000, 750_000, 10000)]
    assert [(a["strike"], a["lots"]) for a in p["put_oi_adds"]] == [
        (23900, 20000), (23800, 5000), (24000, 3000)]
    assert p["spot"] == 24030.0 and p["spot_source"] == "given"
    assert p["atm_strike"] == 24000 and p["straddle"] == 185
    assert p["implied_move_pct"] == pytest.approx(185 / 24030 * 100)
    assert p["future_expiry"] == MONTH and p["basis"] == pytest.approx(80)
    assert p["expiry_today"] is False and "warning" not in p


def test_eod_positioning_spot_falls_back_to_history_then_underlying(tmp_path):
    write_bhav(tmp_path, NOV3, contracts())
    assert facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path)["spot_source"] == \
        "bhavcopy underlying (median)"
    write_history(tmp_path, history())
    p = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path)
    assert p["spot"] == 24030.0 and p["spot_source"] == "history close 2026-11-03"


def test_eod_positioning_skips_the_series_expiring_that_evening(tmp_path):
    write_bhav(tmp_path, NOV3, contracts())
    p = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path, spot=24030.0)
    assert GONE not in (p["front_expiry"], p["next_expiry"])
    assert p["call_wall"] == 24100 and 24000 not in [a["strike"] for a in p["call_oi_adds"][:1]]
    write_bhav(tmp_path, date(2026, 11, 9), [("IDO", "NIFTY", FRONT, 24000, "CE", 1, 10, 0)])
    with pytest.raises(DataError, match="after 2026-11-09"):
        facts.eod_positioning("NIFTY", date(2026, 11, 9), date(2026, 11, 11), root=tmp_path)


def test_eod_positioning_flags_an_expiry_session(tmp_path):
    write_bhav(tmp_path, NOV6, contracts(expired=False))
    p = facts.eod_positioning("NIFTY", NOV6, date(2026, 11, 9), root=tmp_path, spot=24030.0)
    assert p["expiry_today"] is True and p["warning"] == WARNING


def test_eod_positioning_legacy_file_has_no_lots(tmp_path):
    header = ("INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,"
              "SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,")
    rows = ["OPTIDX,NIFTY,25-Jan-2024,21700,CE,1,1,1,100,100,5,1,50000,2500,02-JAN-2024,",
            "OPTIDX,NIFTY,25-Jan-2024,21700,PE,1,1,1,90,90,5,1,75000,5000,02-JAN-2024,"]
    p = fo_bhav.raw_dir(tmp_path) / "2024" / "fo02JAN2024bhav.csv.zip"
    p.parent.mkdir(parents=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("fo02JAN2024bhav.csv", "\n".join([header, *rows]) + "\n")
    out = facts.eod_positioning("NIFTY", date(2024, 1, 2), date(2024, 1, 3), root=tmp_path)
    assert out["lot"] is None and out["put_oi_adds"] == [{"strike": 21700, "oi_change": 5000,
                                                          "lots": None}]
    assert out["spot"] is None and out["straddle"] is None and out["implied_move_pct"] is None


def test_compose_with_every_source_present(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    assert SECTIONS <= set(out) and out["symbol"] == "NIFTY"
    for name in SECTIONS:
        assert "error" not in out[name], (name, out[name])
        assert out[name]["as_of"] and out[name]["source"], name
    json.dumps(out, allow_nan=False)
    s = out["session"]
    assert (s["as_of"], s["for_session"], s["phase"]) == ("2026-11-03", "2026-11-04", "post_close")
    assert s["holidays_year"] == 2026 and s["warnings"] == []
    pr = out["price"]
    assert type(pr["close"]) is float and pr["close"] == 24030.0 and pr["as_of"] == "2026-11-03"
    assert pr["change"] == 130 and pr["change_pct"] == pytest.approx(130 / 23900 * 100)
    assert pr["gap_pct"] == pytest.approx((23950 / 23900 - 1) * 100)
    assert (pr["low"], pr["high"]) == (23920, 24080) and pr["rv5"] > 0 and pr["rv21"] > 0
    assert pr["vix"]["close"] == 14.0 and pr["vix"]["pctile"] == 20.0
    assert pr["vix"]["since"] == "2008-01-02" and "unsettled_bar_dropped" not in pr
    po = out["positioning"]
    assert po["spot"] == 24030.0 and po["max_pain"] == 24000 and po["straddle"] == 185
    fl = out["flows"]
    fii = fl["participant"]["by_participant"]["FII"]
    assert fl["participant"]["as_of"] == "2026-11-03" and fii["idx_fut_net_chg"] == 1000
    fut = fl["fii_index_futures"]
    assert fut["as_of"] == "2026-11-03" and fut["net_cr"] == pytest.approx(fut["buy_cr"] - fut["sell_cr"])
    assert fut["oi_change_contracts"] is None and "2026-11-02" in fut["oi_change_note"]
    assert fl["cash"]["fii"]["net_cr"] == 1250.40 and fl["cash"]["dii"]["net_cr"] == -3678.85
    assert fl["cash"]["as_of"] == "2026-11-03" and "stale" not in fl["cash"]
    ex = out["expiry"]
    assert ex["symbols"]["NIFTY"] == {"next": FRONT, "sessions_left": 4, "expiry_today": False}
    assert ex["symbols"]["BANKNIFTY"] == {"next": MONTH, "sessions_left": 13, "expiry_today": False}
    assert ex["expiry_today"] is False and "warning" not in ex
    ca = out["calendar"]
    assert [r["what"].split(" board")[0] for r in ca["rows"]] == ["BHARTIARTL", "ONGC"]
    assert ca["errors"] == {} and ca["stale"] == []
    assert net == []


def test_compose_with_participant_missing_errors_only_that_item(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch, participant_files=False)
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    assert set(out["flows"]["participant"]) == {"error", "source"}
    assert "not on disk" in out["flows"]["participant"]["error"]
    assert "error" not in out["flows"]["cash"] and "error" not in out["flows"]["fii_index_futures"]
    for name in SECTIONS:
        assert "error" not in out[name], name
    assert not (tmp_path / "participant").exists()
    assert net == []


PART2 = PART.replace("FII,33148,", "FII,32148,")


def write_part_store(root, days):
    path = participant.store_path(root)
    frames = [participant.parse_participant_csv(PART if d == NOV3 else PART2, d) for d in days]
    pd.concat(frames, ignore_index=True).to_parquet(path, index=False)
    return path


def test_compose_reads_participant_flows_from_the_store(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch, participant_files=False)
    path = write_part_store(tmp_path, [NOV2, NOV3])
    before = path.read_bytes()
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    part = out["flows"]["participant"]
    assert part["source"] == "NSE participant store (fao_oi.parquet)"
    assert part["as_of"] == "2026-11-03" and part["prev_date"] == "2026-11-02" and "stale" not in part
    assert part["by_participant"]["FII"]["idx_fut_net_chg"] == 1000
    assert path.read_bytes() == before and not (tmp_path / "participant" / "raw").exists()
    later = datetime(2026, 11, 4, 19, 0, tzinfo=IST)
    write_bhav(tmp_path, NOV4, contracts(expired=False))
    stale = facts.compose("NIFTY", now=later, root=tmp_path, www=NoWWW())["flows"]["participant"]
    assert stale["as_of"] == "2026-11-03" and stale["stale"] is True
    assert net == []


def test_compose_mixes_a_raw_day_with_a_store_day(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch, participant_files=False)
    write_part_store(tmp_path, [NOV2])
    raw = participant.raw_path(NOV3, tmp_path)
    raw.parent.mkdir(parents=True)
    raw.write_text(PART)
    part = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["participant"]
    assert part["source"] == ("NSE fao_participant_oi_03112026.csv; "
                              "2026-11-02 from NSE participant store (fao_oi.parquet)")
    assert part["by_participant"]["FII"]["idx_fut_net_chg"] == 1000


def test_compose_without_the_previous_participant_day_says_so(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch, participant_files=False)
    write_part_store(tmp_path, [NOV3])
    part = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["participant"]
    assert set(part) == {"error", "source"}
    assert part["error"] == "participant OI for 2026-11-02 not on disk, so no change for 2026-11-03"


def test_an_unreadable_participant_store_is_named(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch, participant_files=False)
    participant.store_path(tmp_path).write_bytes(b"not parquet")
    part = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["participant"]
    assert "not on disk" in part["error"] and "participant store unreadable" in part["error"]


def fiidii_for(root, day):
    path = flows.fiidii_path(day, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(FIIDII).replace("03-Nov-2026", f"{day:%d}-Nov-2026"))


def test_cash_never_shows_a_later_session(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    flows.fiidii_path(NOV3, tmp_path).unlink()
    fiidii_for(tmp_path, NOV4)
    cash = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["cash"]
    assert cash == {"error": "no FII/DII cash figures cached on or before 2026-11-03",
                    "source": facts.CASH_SRC}
    fiidii_for(tmp_path, NOV2)
    cash = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["cash"]
    assert cash["as_of"] == "2026-11-02" and cash["stale"] is True
    assert cash["note"] == "2026-11-03 not cached; newest cached on or before it is 2026-11-02"


def test_compose_participant_still_on_yesterday_says_so(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_bhav(tmp_path, NOV4, contracts(expired=False))
    later = datetime(2026, 11, 4, 19, 0, tzinfo=IST)
    out = facts.compose("NIFTY", now=later, root=tmp_path, www=NoWWW())
    assert out["session"]["as_of"] == "2026-11-04"
    part = out["flows"]["participant"]
    assert part["as_of"] == "2026-11-03" and part["stale"] is True and "2026-11-04" in part["note"]
    cash = out["flows"]["cash"]
    assert cash["as_of"] == "2026-11-03" and cash["stale"] is True
    assert out["price"]["stale"] is True and out["price"]["as_of"] == "2026-11-03"
    assert net == []


def test_compose_on_an_expiry_eve_carries_the_warning(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_bhav(tmp_path, NOV6, contracts(expired=False))
    fri = datetime(2026, 11, 6, 20, 0, tzinfo=IST)
    out = facts.compose("NIFTY", now=fri, root=tmp_path, www=NoWWW())
    assert out["session"]["for_session"] == "2026-11-09"
    assert out["expiry"]["expiry_today"] is True and out["expiry"]["warning"] == WARNING
    assert out["positioning"]["expiry_today"] is True and out["positioning"]["warning"] == WARNING
    assert out["expiry"]["symbols"]["BANKNIFTY"]["expiry_today"] is False


def test_compose_drops_a_morning_bar_from_the_archive(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    bar = {"date": pd.Timestamp(NOV4), "open": 24040, "high": 24100, "low": 24000,
           "close": 24090, "volume": 10, "source": "yahoo",
           "captured_at": "2026-11-04T06:30:00+00:00"}
    write_history(tmp_path, history(extra=bar))
    noon = datetime(2026, 11, 4, 12, 30, tzinfo=IST)
    out = facts.compose("NIFTY", now=noon, root=tmp_path, www=NoWWW())
    assert out["session"]["phase"] == "open" and out["session"]["as_of"] == "2026-11-03"
    pr = out["price"]
    assert pr["close"] == 24030.0 and pr["as_of"] == "2026-11-03"
    assert pr["unsettled_bar_dropped"] == {"date": "2026-11-04",
                                           "reason": "captured 12:00 IST, before the 15:30 close"}


class FakeProvider:
    def __init__(self):
        self.calls = []

    def history(self, symbol, period="1y", interval="1d"):
        self.calls.append((symbol, period, interval))
        idx = pd.DatetimeIndex(["2026-11-02", "2026-11-03", "2026-11-04"]).tz_localize(IST)
        return pd.DataFrame({"open": [1.0, 23950, 24040], "high": [1.0, 24080, 24100],
                             "low": [1.0, 23920, 24000], "close": [1.0, 24031.5, 24090],
                             "volume": [1, 1, 1]}, index=idx)


def test_compose_fills_missing_bars_from_the_provider_in_memory(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    arch = write_history(tmp_path, history().iloc[:-1])
    before = arch.read_bytes()
    monkeypatch.setenv("SHUNKAN_OFFLINE", "0")
    prov = FakeProvider()
    noon = datetime(2026, 11, 4, 12, 30, tzinfo=IST)
    out = facts.compose("NIFTY", now=noon, root=tmp_path, provider=prov, www=NoWWW())
    assert prov.calls == [("NIFTY", "1mo", "1d")]
    pr = out["price"]
    assert pr["as_of"] == "2026-11-03" and pr["close"] == 24031.5 and pr["prev_close"] == 23900
    assert "provider" in pr["source"] and "stale" not in pr
    assert arch.read_bytes() == before
    assert sorted(p.name for p in arch.parent.iterdir()) == ["_INDIAVIX.parquet", "_NSEI.parquet"]


class VixProvider(FakeProvider):
    def history(self, symbol, period="1y", interval="1d"):
        if symbol != "INDIAVIX":
            return super().history(symbol, period, interval)
        self.calls.append((symbol, period, interval))
        idx = pd.DatetimeIndex(["2026-11-02", "2026-11-03"]).tz_localize(IST)
        return pd.DataFrame({"open": [12.5, 13.0], "high": [13.0, 16.5], "low": [12.0, 12.8],
                             "close": [12.4, 16.0], "volume": [0, 0]}, index=idx)


def test_compose_fills_a_stale_vix_from_the_provider_in_memory(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    vix = write_history(tmp_path, pd.read_parquet(tmp_path / "history" / "_INDIAVIX.parquet").iloc[:-1],
                        "_INDIAVIX")
    before = vix.read_bytes()
    monkeypatch.setenv("SHUNKAN_OFFLINE", "0")
    prov = VixProvider()
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, provider=prov, www=NoWWW())
    v = out["price"]["vix"]
    assert ("INDIAVIX", "1mo", "1d") in prov.calls
    assert v["as_of"] == "2026-11-03" and v["close"] == 16.0
    assert "provider bars 2026-11-03..2026-11-03 (in memory, not written)" in v["source"]
    assert v["pctile"] == 40.0
    assert vix.read_bytes() == before


class NoProvider:
    def history(self, *a, **k):
        raise AssertionError("provider called")


def test_compose_fills_from_nse_index_files_before_the_provider(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_history(tmp_path, history().iloc[:-1])
    write_ind(tmp_path, NOV3)
    monkeypatch.setenv("SHUNKAN_OFFLINE", "0")
    pr = facts.compose("NIFTY", now=EVENING, root=tmp_path, provider=NoProvider(), www=NoWWW())["price"]
    assert pr["as_of"] == "2026-11-03" and pr["close"] == 24030.0 and pr["prev_close"] == 23900
    assert pr["source"] == "history archive _NSEI.parquet + NSE ind_close_all 2026-11-03..2026-11-03"
    assert "stale" not in pr and "fill_error" not in pr


class GapProvider(FakeProvider):
    def history(self, symbol, period="1y", interval="1d"):
        self.calls.append((symbol, period, interval))
        idx = pd.DatetimeIndex(["2026-11-02", "2026-11-03"]).tz_localize(IST)
        return pd.DataFrame({"open": [23880, 1.0], "high": [23960, 1.0], "low": [23850, 1.0],
                             "close": [23900, 99999.0], "volume": [1, 1]}, index=idx)


def test_the_provider_fills_only_the_days_nse_files_miss(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_history(tmp_path, history().iloc[:-2])
    write_ind(tmp_path, NOV3)
    monkeypatch.setenv("SHUNKAN_OFFLINE", "0")
    prov = GapProvider()
    pr = facts.compose("NIFTY", now=EVENING, root=tmp_path, provider=prov, www=NoWWW())["price"]
    assert prov.calls == [("NIFTY", "1mo", "1d")]
    assert pr["close"] == 24030.0 and pr["prev_close"] == 23900 and pr["prev_date"] == "2026-11-02"
    assert pr["source"] == ("history archive _NSEI.parquet + NSE ind_close_all 2026-11-03..2026-11-03"
                            " + provider bars 2026-11-02..2026-11-02 (in memory, not written)")


def test_compose_offline_never_asks_the_provider(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_history(tmp_path, history().iloc[:-1])

    class Boom:
        def history(self, *a, **k):
            raise AssertionError("provider called offline")

    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, provider=Boom(), www=NoWWW())
    pr = out["price"]
    assert pr["as_of"] == "2026-11-02" and pr["stale"] is True and "2026-11-03" in pr["note"]
    assert "fill_error" not in pr
    assert out["positioning"]["spot_source"] == "bhavcopy underlying (median)"


def test_compose_never_raises_on_an_empty_store(tmp_path, net):
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    assert SECTIONS <= set(out)
    for name in ("session", "price", "positioning", "flows", "expiry"):
        assert set(out[name]) == {"error", "source"}, name
    assert "no F&O bhavcopy" in out["session"]["error"]
    assert out["calendar"]["errors"]["holidays"] == "holidays: not cached yet"
    json.dumps(out, allow_nan=False)
    assert net == []


def test_compose_survives_a_corrupt_archive(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    (tmp_path / "history" / "_NSEI.parquet").write_bytes(b"not parquet")
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    assert set(out["price"]) == {"error", "source"}
    assert "error" not in out["positioning"] and out["positioning"]["spot_source"] == \
        "bhavcopy underlying (median)"


def test_compose_warns_without_a_holiday_list(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    (tmp_path / "calendar" / "holidays.json").unlink()
    out = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    s = out["session"]
    assert s["holidays_year"] is None and any("not cached" in w for w in s["warnings"])
    assert out["expiry"]["symbols"]["BANKNIFTY"]["sessions_left"] == 14


def test_compose_names_a_stale_holiday_list_and_the_holidays_skipped(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_bhav(tmp_path, date(2026, 11, 9), [("IDO", "NIFTY", NEXT, 24000, "CE", 1, 10, 0),
                                              ("IDO", "NIFTY", NEXT, 24000, "PE", 1, 10, 0)])
    out = facts.compose("NIFTY", now=datetime(2026, 11, 9, 20, 0, tzinfo=IST), root=tmp_path,
                        www=NoWWW())
    s = out["session"]
    assert (s["as_of"], s["for_session"], s["holidays_skipped"]) == (
        "2026-11-09", "2026-11-11", ["2026-11-10"])
    late = facts.compose("NIFTY", now=datetime(2026, 11, 11, 20, 0, tzinfo=IST), root=tmp_path,
                         www=NoWWW())
    assert late["session"]["warnings"] == [
        "holiday list fetched 2026-11-03, older than 7 days",
        "today's bhavcopy not on disk yet; the 2026-11-11 session has closed"]


def test_compose_after_the_close_without_todays_bhavcopy_says_so(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    s = facts.compose("NIFTY", now=datetime(2026, 11, 4, 19, 0, tzinfo=IST), root=tmp_path,
                      www=NoWWW())["session"]
    assert s["phase"] == "post_close" and s["as_of"] == "2026-11-03"
    assert s["warnings"] == ["today's bhavcopy not on disk yet; the 2026-11-04 session has closed"]
    s = facts.compose("NIFTY", now=datetime(2026, 11, 4, 15, 0, tzinfo=IST), root=tmp_path,
                      www=NoWWW())["session"]
    assert s["phase"] == "open" and s["warnings"] == []


def test_a_corrupt_raw_participant_file_falls_through_to_the_store(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch, participant_files=False)
    write_part_store(tmp_path, [NOV2, NOV3])
    raw = participant.raw_path(NOV3, tmp_path)
    raw.parent.mkdir(parents=True)
    raw.write_text("<html>Access Denied</html>")
    part = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["participant"]
    assert part["source"] == facts.PART_STORE_SRC and part["as_of"] == "2026-11-03"
    assert part["by_participant"]["FII"]["idx_fut_net_chg"] == 1000
    write_part_store(tmp_path, [NOV2])
    part = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["flows"]["participant"]
    assert "fao_participant_oi_03112026.csv unreadable" in part["error"]


def test_a_missing_expiry_symbol_names_its_source(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_bhav(tmp_path, NOV3, [r for r in contracts() if r[1] != "BANKNIFTY"])
    ex = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())["expiry"]
    assert ex["symbols"]["BANKNIFTY"] == {
        "error": "no BANKNIFTY contract expiring after 2026-11-03 in the bhavcopy",
        "source": "NSE F&O bhavcopy 2026-11-03 (end of day)"}
    assert ex["symbols"]["NIFTY"]["next"] == FRONT


def test_walls_are_out_of_the_money(tmp_path):
    rows = contracts() + [("IDO", "NIFTY", FRONT, 24300, "PE", 300, 9_000_000, 0),
                          ("IDO", "NIFTY", FRONT, 23700, "CE", 340, 9_000_000, 0)]
    write_bhav(tmp_path, NOV3, rows)
    p = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path, spot=24030.0)
    assert (p["call_wall"], p["put_wall"]) == (24100, 23900)
    assert "call_wall_note" not in p and "put_wall_note" not in p
    hi = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path, spot=30000.0)
    assert hi["call_wall"] is None and hi["call_wall_note"] == "no call strike at or above spot 30000.0"
    assert hi["put_wall"] == 24300
    lo = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path, spot=20000.0)
    assert lo["put_wall"] is None and lo["put_wall_note"] == "no put strike at or below spot 20000.0"
    assert lo["call_wall"] == 23700


def test_a_nan_spot_is_treated_as_missing(tmp_path):
    side = pd.DataFrame({"oi": [1.0]}, index=[24000.0])
    assert facts._wall(side, float("nan"), "CE") == (None, "no spot, so no out-of-the-money side")
    write_bhav(tmp_path, NOV3, contracts())
    p = facts.eod_positioning("NIFTY", NOV3, NOV4, root=tmp_path, spot=float("nan"))
    assert p["spot"] == 24025.0 and p["spot_source"] == "bhavcopy underlying (median)"
    assert (p["call_wall"], p["put_wall"], p["atm_strike"], p["straddle"]) == (24100, 23900, 24000, 185)


def test_compose_banknifty_uses_its_monthly(tmp_path, monkeypatch, net):
    store(tmp_path, monkeypatch)
    write_history(tmp_path, history(), "_NSEBANK")
    out = facts.compose("banknifty", now=EVENING, root=tmp_path, www=NoWWW())
    assert out["symbol"] == "BANKNIFTY"
    po = out["positioning"]
    assert po["front_expiry"] == MONTH and po["next_expiry"] is None and po["pcr_next"] is None
    assert po["pcr_front"] == pytest.approx(420 / 315) and po["lot"] == 35
    assert out["expiry"]["expiry_today"] is False


def archive_client(routes, seen=None):
    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        if seen is not None:
            seen.append(name)
        status, body = routes.get(name, (404, b"Not Found"))
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


BHAV3 = "BhavCopy_NSE_FO_0_0_0_20261103_F_0000.csv.zip"
IND3 = "ind_close_all_03112026.csv"


def ind_close_all(day, nifty=(23950, 24080, 23920, 24030), bank=(51000, 51500, 50800, 51400), vix=14.0):
    head = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
            "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n")
    row = lambda name, o, h, lo, c: f"{name},{day:%d-%m-%Y},{o},{h},{lo},{c},0,0,-,-,-,-,-\n"  # noqa: E731
    return (head + row("Nifty 50", *nifty) + row("Nifty Bank", *bank)
            + row("India VIX", vix, vix + 1, vix - 1, vix)).encode()


def write_ind(root, day, **kw):
    p = index_bars.path_for(day, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(ind_close_all(day, **kw))
    return p


def test_refresh_splits_unpublished_from_failed(tmp_path, monkeypatch):
    members(monkeypatch)
    client = archive_client({"fii_stats_03-Nov-2026.xls": (200, XLS),
                             "fao_participant_oi_03112026.csv": (200, DENIED)})
    out = facts.refresh(now=EVENING, root=tmp_path, client=client, www=FakeWWW())
    assert out["session"] == "2026-11-03" and out["complete"] is False
    src = out["sources"]
    assert src["bhavcopy"].startswith("unpublished:") and "404" in src["bhavcopy"]
    assert src["participant"].startswith("failed:")
    assert src["index_close"].startswith("unpublished:")
    assert {k: v for k, v in src.items() if k not in ("bhavcopy", "participant", "index_close")} == {
        "holidays": "ok", "fii_derivatives": "ok", "fii_dii_cash": "ok",
        "board_meetings": "ok", "universe": "ok"}
    assert not participant.raw_path(NOV3, tmp_path).exists()


def test_refresh_then_compose_reads_what_it_fetched(tmp_path, monkeypatch, net):
    members(monkeypatch)
    seen = []
    client = archive_client({BHAV3: (200, bhav_bytes(NOV3, contracts())),
                             "fao_participant_oi_03112026.csv": (200, PART.encode()),
                             "fao_participant_oi_02112026.csv":
                                 (200, PART.replace("FII,33148,", "FII,32148,").encode()),
                             "fii_stats_03-Nov-2026.xls": (200, XLS),
                             IND3: (200, ind_close_all(NOV3))}, seen)
    www = FakeWWW()
    out = facts.refresh(now=EVENING, root=tmp_path, client=client, www=www)
    assert out == {"session": "2026-11-03", "complete": True, "sources": dict.fromkeys(
        ["holidays", "bhavcopy", "index_close", "participant", "fii_derivatives", "fii_dii_cash",
         "board_meetings", "universe"], "ok")}
    assert index_bars.path_for(NOV3, tmp_path).read_bytes() == ind_close_all(NOV3)
    assert "fao_participant_oi_02112026.csv" in seen and "fii_stats_02-Nov-2026.xls" in seen
    assert www.calls == [cal.HOLIDAYS_PATH, flows.FIIDII_PATH, cal.BOARD_PATH]
    write_history(tmp_path, history())
    card = facts.compose("NIFTY", now=EVENING, root=tmp_path, www=NoWWW())
    for name in SECTIONS:
        assert "error" not in card[name], name
    assert card["flows"]["participant"]["by_participant"]["FII"]["idx_fut_net_chg"] == 1000
    assert [r["what"].split(" board")[0] for r in card["calendar"]["rows"]] == ["BHARTIARTL", "ONGC"]
    again = facts.refresh(now=EVENING + timedelta(minutes=20), root=tmp_path, client=client, www=www)
    assert again["complete"] is True and www.calls == [cal.HOLIDAYS_PATH, flows.FIIDII_PATH,
                                                       cal.BOARD_PATH]
    assert net == []


def test_refresh_names_an_old_cash_session(tmp_path, monkeypatch):
    members(monkeypatch)
    later = datetime(2026, 11, 4, 20, 0, tzinfo=IST)
    out = facts.refresh(now=later, root=tmp_path, client=archive_client({}), www=FakeWWW())
    assert out["session"] == "2026-11-04"
    assert out["sources"]["fii_dii_cash"] == "unpublished: latest is 2026-11-03"
    assert flows.fii_dii_cash_on(NOV3, tmp_path) is not None


def test_refresh_before_the_close_targets_the_previous_session(tmp_path, monkeypatch):
    members(monkeypatch)
    out = facts.refresh(now=datetime(2026, 11, 4, 15, 0, tzinfo=IST), root=tmp_path,
                        client=archive_client({}), www=FakeWWW())
    assert out["session"] == "2026-11-03"
    out = facts.refresh(now=datetime(2026, 11, 11, 9, 0, tzinfo=IST), root=tmp_path,
                        client=archive_client({}), www=FakeWWW())
    assert out["session"] == "2026-11-09"


def test_refresh_never_raises(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("constituents down")

    monkeypatch.setattr(constituents, "universe", broken)
    www = FakeWWW({cal.HOLIDAYS_PATH: DataError("blocked"), cal.BOARD_PATH: DataError("blocked"),
                   flows.FIIDII_PATH: DataError("blocked")})
    out = facts.refresh(now=EVENING, root=tmp_path, client=archive_client({}), www=www)
    assert out["complete"] is False and out["session"] == "2026-11-03"
    assert out["sources"]["holidays"] == "failed: blocked"
    assert out["sources"]["universe"] == "failed: RuntimeError: constituents down"

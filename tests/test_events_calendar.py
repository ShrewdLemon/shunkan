"""The 48-hour calendar, offline: holidays, board meetings, macro dates, expiries."""

from __future__ import annotations

import json
import tomllib
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from shunkan.data import constituents, fo_bhav
from shunkan.data import events_calendar as cal
from shunkan.data.provider import DataError, FetchFailed
from shunkan.markets import IST

FIX = Path(__file__).parent / "fixtures"
HOLIDAYS = json.loads((FIX / "holidays.json").read_text())
BOARD = json.loads((FIX / "board_meetings.json").read_text())
NOW = datetime(2026, 11, 4, 18, 0, tzinfo=IST)


class FakeWWW:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        out = self.routes[path]
        if isinstance(out, Exception):
            raise out
        return out


def _www(holidays=HOLIDAYS, board=BOARD):
    return FakeWWW({cal.HOLIDAYS_PATH: holidays, cal.BOARD_PATH: board})


def test_holidays_parse_the_fo_rows(tmp_path):
    hols = cal.holidays(root=tmp_path, www=_www(), now=NOW)
    assert date(2026, 11, 10) in hols and date(2026, 11, 24) in hols
    assert date(2026, 11, 9) not in hols
    assert hols == sorted(hols) and len(hols) == 20


def test_holidays_ask_for_the_trading_list(tmp_path):
    www = _www()
    cal.holidays(root=tmp_path, www=www, now=NOW)
    assert www.calls == [(cal.HOLIDAYS_PATH, {"type": "trading"})]


def test_holidays_cache_for_seven_days(tmp_path):
    www = _www()
    first = cal.holidays(root=tmp_path, www=www, now=NOW)
    cached = json.loads((tmp_path / "calendar" / "holidays.json").read_text())
    assert set(cached) == {"fetched_at", "payload"} and cached["payload"] == HOLIDAYS
    assert cal.holidays(root=tmp_path, www=www, now=NOW + timedelta(days=6)) == first
    assert len(www.calls) == 1
    cal.holidays(root=tmp_path, www=www, now=NOW + timedelta(days=8))
    assert len(www.calls) == 2


def test_stale_holiday_cache_is_used_when_the_refresh_fails(tmp_path):
    first = cal.holidays(root=tmp_path, www=_www(), now=NOW)
    down = _www(holidays=FetchFailed("blocked: HTTP 403 after re-warm"))
    later = NOW + timedelta(days=30)
    assert cal.holidays(root=tmp_path, www=down, now=later) == first
    assert cal.holidays_fetched_at(root=tmp_path) == NOW


def test_holidays_without_a_cache_raise_when_the_fetch_fails(tmp_path):
    with pytest.raises(FetchFailed, match="blocked"):
        cal.holidays(root=tmp_path, www=_www(holidays=FetchFailed("blocked")), now=NOW)
    assert cal.holidays_fetched_at(root=tmp_path) is None


@pytest.mark.parametrize("payload", [
    {"CM": HOLIDAYS["CM"]},
    {"FO": []},
    {"FO": [{"tradingDate": "10/11/2026"}]},
    [HOLIDAYS],
])
def test_bad_holiday_payload_fails_and_is_not_cached(tmp_path, payload):
    with pytest.raises(FetchFailed, match="holiday-master"):
        cal.holidays(root=tmp_path, www=_www(holidays=payload), now=NOW)
    assert not (tmp_path / "calendar" / "holidays.json").exists()


def test_sessions_skip_weekends_and_holidays(tmp_path):
    hols = cal.holidays(root=tmp_path, www=_www(), now=NOW)
    assert cal.next_session(date(2026, 11, 6), hols) == date(2026, 11, 9)
    assert cal.next_session(date(2026, 11, 9), hols) == date(2026, 11, 11)
    assert cal.prev_session(date(2026, 11, 11), hols) == date(2026, 11, 9)
    assert cal.prev_session(date(2026, 11, 9), hols) == date(2026, 11, 6)
    assert not cal.is_trading_day(date(2026, 11, 10), hols)
    assert not cal.is_trading_day(date(2026, 11, 7), hols)
    assert cal.is_trading_day(date(2026, 11, 9), hols)


def test_board_meetings_dedupe_filings_and_join_purposes(tmp_path):
    www = _www()
    out = cal.board_meetings(date(2026, 11, 5), date(2026, 11, 6), root=tmp_path, www=www, now=NOW)
    assert www.calls == [(cal.BOARD_PATH, {"index": "equities", "from_date": "05-11-2026",
                                           "to_date": "06-11-2026"})]
    hind = [r for r in out if r["symbol"] == "HINDALCO"]
    assert hind == [{"symbol": "HINDALCO", "date": date(2026, 11, 6),
                     "purpose": "Board Meeting Intimation; Financial Results"}]
    bharti = [r for r in out if r["symbol"] == "BHARTIARTL"]
    assert len(bharti) == 1 and bharti[0]["purpose"] == "Board Meeting Intimation; Financial Results"
    assert len(out) == len({(r["symbol"], r["date"]) for r in out}) == 10
    assert [(r["date"], r["symbol"]) for r in out] == sorted((r["date"], r["symbol"]) for r in out)


def test_board_meetings_keep_only_the_asked_dates(tmp_path):
    out = cal.board_meetings(date(2026, 11, 6), date(2026, 11, 6), root=tmp_path, www=_www(), now=NOW)
    assert {r["date"] for r in out} == {date(2026, 11, 6)} and "BHARTIARTL" not in {r["symbol"] for r in out}


def test_board_meetings_cache_for_six_hours(tmp_path):
    www = _www()
    span = (date(2026, 11, 5), date(2026, 11, 6))
    first = cal.board_meetings(*span, root=tmp_path, www=www, now=NOW)
    cached = json.loads((tmp_path / "calendar" / "board" / "20261104.json").read_text())
    assert cached["payload"] == BOARD
    assert cal.board_meetings(*span, root=tmp_path, www=www, now=NOW + timedelta(hours=5)) == first
    assert len(www.calls) == 1
    cal.board_meetings(*span, root=tmp_path, www=www, now=NOW + timedelta(hours=6, minutes=1))
    assert len(www.calls) == 2


def test_board_cache_for_a_narrower_span_is_refetched(tmp_path):
    www = _www()
    cal.board_meetings(date(2026, 11, 5), date(2026, 11, 5), root=tmp_path, www=www, now=NOW)
    cal.board_meetings(date(2026, 11, 5), date(2026, 11, 6), root=tmp_path, www=www, now=NOW)
    assert len(www.calls) == 2


@pytest.mark.parametrize("payload", [{"error": "x"}, [{"bm_symbol": "X"}],
                                     [{"bm_symbol": "X", "bm_date": "6/11/2026", "bm_purpose": ""}]])
def test_bad_board_payload_fails_and_is_not_cached(tmp_path, payload):
    with pytest.raises(FetchFailed, match="corporate-board-meetings"):
        cal.board_meetings(date(2026, 11, 5), date(2026, 11, 6), root=tmp_path,
                           www=_www(board=payload), now=NOW)
    assert not (tmp_path / "calendar" / "board").exists()


def test_macro_events_carry_us_cpi_at_19_ist():
    out = cal.macro_events(datetime(2026, 11, 10, tzinfo=IST), datetime(2026, 11, 11, tzinfo=IST))
    assert [(r["at_ist"], r["series"], r["event"]) for r in out["rows"]] == [
        ("2026-11-10T19:00:00+05:30", "us_cpi", "US CPI")]
    assert out["rows"][0]["source"] == "https://www.bls.gov/schedule/news_release/cpi.htm"
    assert out["stale"] == []


def test_macro_coverage_marks_a_series_stale_past_its_end():
    start = datetime(2027, 2, 1, tzinfo=IST)
    out = cal.macro_events(start, datetime(2027, 2, 6, 9, 0, tzinfo=IST))
    assert out["stale"] == ["rbi_mpc"]
    assert out["coverage"] == {"rbi_mpc": "2027-02-05", "fomc": "2027-12-09",
                               "us_cpi": "2027-12-10", "india_cpi": "2027-03-12"}
    assert cal.macro_events(start, datetime(2027, 2, 5, 23, 0, tzinfo=IST))["stale"] == []
    assert cal.macro_events(start, datetime(2028, 1, 1, tzinfo=IST))["stale"] == [
        "fomc", "india_cpi", "rbi_mpc", "us_cpi"]


def test_packaged_macro_calendar_is_whole_and_within_coverage():
    data = cal.load_macro()
    assert len(data["events"]) == 30 and "gaps" in data
    cov = cal.macro_events(datetime(2026, 1, 1, tzinfo=IST), datetime(2026, 1, 2, tzinfo=IST))["coverage"]
    for e in data["events"]:
        series = cal.SERIES[e["event"]]
        assert e["date"] <= cov[series] and e["source"].startswith("https://")


def test_macro_calendar_and_repo_table_ship_as_package_data():
    py = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert py["tool"]["setuptools"]["package-data"]["shunkan.data"] == ["*.json", "*.csv"]


UDIFF_COLS = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
              "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
              "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty",
              "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]


def _bhav(root, day, contracts):
    rows = []
    for tp, sym, expiry, strike, right in contracts:
        v = dict.fromkeys(UDIFF_COLS, "")
        v.update(TradDt=day.isoformat(), BizDt=day.isoformat(), Sgmt="FO", Src="NSE",
                 FinInstrmTp=tp, TckrSymb=sym, XpryDt=expiry, FininstrmActlXpryDt=expiry,
                 StrkPric=strike, OptnTp=right, OpnPric="1", HghPric="1", LwPric="1",
                 ClsPric="1", SttlmPric="1", OpnIntrst="10", ChngInOpnIntrst="0",
                 TtlTradgVol="1", UndrlygPric="24000", NewBrdLotQty="75", SsnId="F1")
        rows.append(",".join(v[k] for k in UDIFF_COLS))
    name = f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv"
    p = fo_bhav.raw_dir(root) / f"{day:%Y}" / (name + ".zip")
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(name, "\n".join([",".join(UDIFF_COLS), *rows]) + "\n")
    return p


FRI = date(2026, 11, 6)
CONTRACTS = [
    ("IDO", "NIFTY", "2026-11-09", "24000.00", "CE"),
    ("IDO", "NIFTY", "2026-11-09", "24000.00", "PE"),
    ("IDO", "NIFTY", "2026-11-17", "24000.00", "CE"),
    ("IDF", "NIFTY", "2026-11-24", "", ""),
    ("IDO", "BANKNIFTY", "2026-11-24", "52000.00", "CE"),
    ("IDO", "FINNIFTY", "2026-11-09", "23000.00", "CE"),
    ("STO", "RELIANCE", "2026-11-24", "1400.00", "CE"),
]


def test_expiries_are_distinct_ascending_per_symbol(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    assert cal.expiries(FRI, root=tmp_path) == {
        "NIFTY": [date(2026, 11, 9), date(2026, 11, 17), date(2026, 11, 24)],
        "BANKNIFTY": [date(2026, 11, 24)]}


WINDOW_NOW = datetime(2026, 11, 8, 20, 0, tzinfo=IST)
WINDOW_BOARD = [
    {"bm_symbol": "HINDALCO", "bm_date": "09-Nov-2026", "bm_purpose": "Financial Results"},
    {"bm_symbol": "3MINDIA", "bm_date": "09-Nov-2026", "bm_purpose": "Financial Results"},
    {"bm_symbol": "ONGC", "bm_date": "13-Nov-2026", "bm_purpose": "Financial Results"},
]


def test_window_merges_all_four_kinds_in_time_order(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    out = cal.window(WINDOW_NOW, universe={"HINDALCO", "ONGC"}, root=tmp_path,
                     www=_www(board=WINDOW_BOARD))
    assert out["from"] == "2026-11-08T20:00:00+05:30" and out["to"] == "2026-11-10T20:00:00+05:30"
    assert [(r["at_ist"], r["kind"]) for r in out["rows"]] == [
        ("2026-11-09T09:15:00+05:30", "board_meeting"),
        ("2026-11-09T15:30:00+05:30", "expiry"),
        ("2026-11-10T09:15:00+05:30", "holiday"),
        ("2026-11-10T19:00:00+05:30", "macro"),
    ]
    what = {r["kind"]: r["what"] for r in out["rows"]}
    assert "HINDALCO" in what["board_meeting"] and "Financial Results" in what["board_meeting"]
    assert "NIFTY" in what["expiry"] and "Diwali-Balipratipada" in what["holiday"]
    assert what["macro"].startswith("US CPI")
    assert all(set(r) == {"at_ist", "kind", "what", "source"} and r["source"] for r in out["rows"])
    assert out["errors"] == {} and out["stale"] == []
    assert out["coverage"]["rbi_mpc"] == "2027-02-05"


def test_window_renders_the_rest_when_board_meetings_fail(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    www = _www(board=FetchFailed("blocked: HTTP 403 after re-warm"))
    out = cal.window(WINDOW_NOW, universe={"HINDALCO"}, root=tmp_path, www=www)
    assert set(out["errors"]) == {"board_meetings"} and "blocked" in out["errors"]["board_meetings"]
    assert [r["kind"] for r in out["rows"]] == ["expiry", "holiday", "macro"]


def test_window_names_a_stale_holiday_list(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    cal.holidays(root=tmp_path, www=_www(), now=WINDOW_NOW - timedelta(days=20))
    www = _www(holidays=FetchFailed("blocked"), board=WINDOW_BOARD)
    out = cal.window(WINDOW_NOW, universe={"HINDALCO"}, root=tmp_path, www=www)
    assert "holiday" in [r["kind"] for r in out["rows"]]
    assert "2026-10-19" in out["errors"]["holidays"] and "blocked" in out["errors"]["holidays"]


def test_window_without_a_bhavcopy_reports_expiries(tmp_path):
    out = cal.window(WINDOW_NOW, universe={"HINDALCO"}, root=tmp_path, www=_www(board=WINDOW_BOARD))
    assert "expiries" in out["errors"]
    assert [r["kind"] for r in out["rows"]] == ["board_meeting", "holiday", "macro"]


class NoWWW:
    def get_json(self, path, params=None):
        raise AssertionError(f"fetch=False touched www: {path}")


def _warm(root, now):
    cal.holidays(root=root, www=_www(), now=now)
    cal.board_meetings(now.date(), (now + timedelta(hours=48)).date(), root=root,
                       www=_www(board=WINDOW_BOARD), now=now)


def test_cache_only_reads_never_touch_www(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    _warm(tmp_path, WINDOW_NOW - timedelta(hours=1))
    assert date(2026, 11, 10) in cal.holidays(root=tmp_path, www=NoWWW(), now=WINDOW_NOW, fetch=False)
    out = cal.board_meetings(date(2026, 11, 8), date(2026, 11, 10), root=tmp_path, www=NoWWW(),
                             now=WINDOW_NOW, fetch=False)
    assert [r["symbol"] for r in out] == ["3MINDIA", "HINDALCO"]
    win = cal.window(WINDOW_NOW, universe={"HINDALCO"}, root=tmp_path, www=NoWWW(), fetch=False)
    assert [r["kind"] for r in win["rows"]] == ["board_meeting", "expiry", "holiday", "macro"]
    assert win["errors"] == {}


def test_cache_only_labels_stale_caches(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    _warm(tmp_path, WINDOW_NOW - timedelta(days=20))
    cal.board_meetings(date(2026, 11, 8), date(2026, 11, 10), root=tmp_path,
                       www=_www(board=WINDOW_BOARD), now=WINDOW_NOW - timedelta(hours=8))
    win = cal.window(WINDOW_NOW, universe={"HINDALCO"}, root=tmp_path, www=NoWWW(), fetch=False)
    assert [r["kind"] for r in win["rows"]] == ["board_meeting", "expiry", "holiday", "macro"]
    assert win["errors"]["holidays"] == "holiday list fetched 2026-10-19, older than 7 days"
    assert "fetched 2026-11-08 12:00" in win["errors"]["board_meetings"]


def test_cache_only_uses_an_earlier_days_board_cache_and_names_a_short_span(tmp_path):
    cal.board_meetings(date(2026, 11, 8), date(2026, 11, 9), root=tmp_path,
                       www=_www(board=WINDOW_BOARD), now=WINDOW_NOW - timedelta(days=1))
    later = WINDOW_NOW + timedelta(hours=4)
    win = cal.window(later, universe={"HINDALCO"}, root=tmp_path, www=NoWWW(), fetch=False)
    assert "2026-11-08..2026-11-09" in win["errors"]["board_meetings"]
    assert "2026-11-09..2026-11-11" in win["errors"]["board_meetings"]


def test_cache_only_with_nothing_cached_says_so(tmp_path):
    with pytest.raises(DataError, match="holidays: not cached yet"):
        cal.holidays(root=tmp_path, www=NoWWW(), now=WINDOW_NOW, fetch=False)
    with pytest.raises(DataError, match="board meetings: not cached yet"):
        cal.board_meetings(date(2026, 11, 8), date(2026, 11, 10), root=tmp_path, www=NoWWW(),
                           now=WINDOW_NOW, fetch=False)
    _bhav(tmp_path, FRI, CONTRACTS)
    win = cal.window(WINDOW_NOW, universe={"HINDALCO"}, root=tmp_path, www=NoWWW(), fetch=False)
    assert win["errors"] == {"holidays": "holidays: not cached yet",
                             "board_meetings": "board meetings: not cached yet"}
    assert [r["kind"] for r in win["rows"]] == ["expiry", "macro"]


def _no_network(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise AssertionError("network call")

    monkeypatch.setattr(constituents, "fetch_constituents", boom)
    monkeypatch.setattr(httpx, "get", boom)


def test_cache_only_window_without_a_cached_universe_makes_no_network_call(tmp_path, monkeypatch):
    _warm(tmp_path, WINDOW_NOW - timedelta(hours=1))
    _no_network(monkeypatch)
    monkeypatch.setattr(constituents, "_mem", {})
    win = cal.window(WINDOW_NOW, root=tmp_path, www=NoWWW(), fetch=False)
    assert win["errors"]["board_meetings"] == "universe not cached"
    assert "board_meeting" not in [r["kind"] for r in win["rows"]]


def test_cache_only_window_uses_the_cached_universe(tmp_path, monkeypatch):
    _warm(tmp_path, WINDOW_NOW - timedelta(hours=1))
    _no_network(monkeypatch)
    c = constituents.Constituent
    monkeypatch.setattr(constituents, "_mem", {
        "NIFTY50": (0.0, [c("HINDALCO", "Hindalco Industries", ("NIFTY50",))]),
        "BANKNIFTY": (0.0, [c("SBIN", "State Bank of India", ("BANKNIFTY",))])})
    win = cal.window(WINDOW_NOW, root=tmp_path, www=NoWWW(), fetch=False)
    assert [r["what"] for r in win["rows"] if r["kind"] == "board_meeting"] == [
        "HINDALCO board meeting: Financial Results"]


def test_holidays_year_names_the_cached_list(tmp_path):
    assert cal.holidays_year(root=tmp_path) is None
    cal.holidays(root=tmp_path, www=_www(), now=NOW)
    assert cal.holidays_year(root=tmp_path) == 2026


def test_window_skips_a_weekend_holiday_row(tmp_path):
    _bhav(tmp_path, FRI, CONTRACTS)
    sat = datetime(2026, 11, 7, 20, 0, tzinfo=IST)
    out = cal.window(sat, universe={"HINDALCO"}, root=tmp_path, www=_www(board=WINDOW_BOARD))
    assert "Laxmi Pujan" not in " ".join(r["what"] for r in out["rows"])
    assert [r["kind"] for r in out["rows"]] == ["board_meeting", "expiry"]


def test_window_flags_a_bhavcopy_older_than_the_last_session(tmp_path):
    _bhav(tmp_path, date(2026, 11, 3), [("IDO", "NIFTY", "2026-11-09", "24000.00", "CE")])
    thu = datetime(2026, 11, 5, 20, 0, tzinfo=IST)
    out = cal.window(thu, universe=set(), root=tmp_path, www=_www(board=[]))
    assert out["errors"]["expiries"] == "bhavcopy on disk is 2026-11-03; expiries may be incomplete"
    later = cal.window(datetime(2026, 11, 7, 20, 0, tzinfo=IST), universe=set(), root=tmp_path,
                       www=_www(board=[]))
    assert [r["what"] for r in later["rows"] if r["kind"] == "expiry"] == ["NIFTY expiry"]


def _members(monkeypatch, calls=None):
    c = constituents.Constituent
    lists = {"NIFTY50": [c("HINDALCO", "Hindalco Industries", ("NIFTY50",))],
             "BANKNIFTY": [c("SBIN", "State Bank of India", ("BANKNIFTY",))]}

    def fake(index_name):
        if calls is not None:
            calls.append(index_name)
        return lists[index_name]

    monkeypatch.setattr(constituents, "fetch_constituents", fake)


def test_fetching_the_universe_saves_it_for_cold_processes(tmp_path, monkeypatch):
    _members(monkeypatch)
    assert cal.index_universe(root=tmp_path, now=NOW) == {"HINDALCO", "SBIN"}
    saved = json.loads((tmp_path / "calendar" / "universe.json").read_text())
    assert saved == {"date": "2026-11-04", "symbols": ["HINDALCO", "SBIN"]}
    _no_network(monkeypatch)
    monkeypatch.setattr(constituents, "_mem", {})
    assert cal.index_universe(root=tmp_path, now=NOW, fetch=False) == {"HINDALCO", "SBIN"}


def test_cache_only_window_reads_the_saved_universe(tmp_path, monkeypatch):
    _members(monkeypatch)
    _warm(tmp_path, WINDOW_NOW - timedelta(hours=1))
    cal.window(WINDOW_NOW - timedelta(hours=1), root=tmp_path, www=_www(board=WINDOW_BOARD))
    assert (tmp_path / "calendar" / "universe.json").exists()
    _no_network(monkeypatch)
    monkeypatch.setattr(constituents, "_mem", {})
    win = cal.window(WINDOW_NOW, root=tmp_path, www=NoWWW(), fetch=False)
    assert [r["what"] for r in win["rows"] if r["kind"] == "board_meeting"] == [
        "HINDALCO board meeting: Financial Results"]


def test_universe_file_is_rewritten_only_when_the_day_or_members_change(tmp_path, monkeypatch):
    _members(monkeypatch)
    path = tmp_path / "calendar" / "universe.json"
    cal.index_universe(root=tmp_path, now=NOW)
    first = path.stat().st_mtime_ns
    cal.index_universe(root=tmp_path, now=NOW + timedelta(hours=2))
    assert path.stat().st_mtime_ns == first
    cal.index_universe(root=tmp_path, now=NOW + timedelta(days=1))
    assert json.loads(path.read_text())["date"] == "2026-11-05"

import re
import zipfile
from datetime import date

import pandas as pd
import pytest

from shunkan.data import fo_bhav
from shunkan.data.provider import DataError

LEGACY_HEADER = ("INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,"
                 "SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,")
LEGACY_ROWS = [
    "FUTIDX,NIFTY,25-Jan-2024,0,XX,21700,21800,21600,21750.5,21750.5,1000,1000,500000,2500,02-JAN-2024,",
    "OPTIDX,NIFTY,25-Jan-2024,21700,CE,200,220,180,210.5,210.5,300,10,75000,-500,02-JAN-2024,",
    "OPTIDX,NIFTY,25-Jan-2024,21700,PE,150,160,140,155,155,0,0,90000,0,02-JAN-2024,",
    "OPTSTK,RELIANCE,25-Jan-2024,2600,CE,30,31,29,30.5,30.5,12,1,25000,250,02-JAN-2024,",
]

UDIFF_COLS = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
              "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
              "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty",
              "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]


def _udiff_row(tp, sym, strike, right, o, h, l, c, s, oi, chg, vol, lot):
    v = dict.fromkeys(UDIFF_COLS, "")
    v.update(TradDt="2024-01-02", BizDt="2024-01-02", Sgmt="FO", Src="NSE",
             FinInstrmTp=tp, TckrSymb=sym, XpryDt="2024-01-25",
             FininstrmActlXpryDt="2024-01-25", StrkPric=strike, OptnTp=right,
             OpnPric=o, HghPric=h, LwPric=l, ClsPric=c, SttlmPric=s,
             OpnIntrst=oi, ChngInOpnIntrst=chg, TtlTradgVol=vol,
             UndrlygPric="21720.00", NewBrdLotQty=lot, SsnId="F1")
    return ",".join(v[k] for k in UDIFF_COLS)


UDIFF_ROWS = [
    _udiff_row("IDF", "NIFTY", "", "", "21700", "21800", "21600", "21750.5", "21750.5", "500000", "2500", "1000", "25"),
    _udiff_row("IDO", "NIFTY", "21700.00", "CE", "200", "220", "180", "210.5", "210.5", "75000", "-500", "300", "25"),
    _udiff_row("IDO", "NIFTY", "21700.00", "PE", "150", "160", "140", "155", "155", "90000", "0", "0", "25"),
    _udiff_row("STO", "RELIANCE", "2600.00", "CE", "30", "31", "29", "30.5", "30.5", "25000", "250", "12", "250"),
]


def _zip(path, name, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, text)
    return path


def legacy_zip(root, day=date(2024, 1, 2), rows=LEGACY_ROWS):
    mon = day.strftime("%b").upper()
    p = fo_bhav.raw_dir(root) / f"{day:%Y}" / f"fo{day:%d}{mon}{day:%Y}bhav.csv.zip"
    return _zip(p, p.name[:-4], "\n".join([LEGACY_HEADER, *rows]) + "\n")


def udiff_zip(root, day=date(2024, 1, 2), rows=UDIFF_ROWS):
    p = fo_bhav.raw_dir(root) / f"{day:%Y}" / f"BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    return _zip(p, p.name[:-4], "\n".join([",".join(UDIFF_COLS), *rows]) + "\n")


def test_raw_dir_is_under_root(tmp_path):
    assert fo_bhav.raw_dir(tmp_path) == tmp_path / "fo_bhav" / "raw"


def test_both_forms_parse_to_the_same_frame(tmp_path):
    a = fo_bhav.parse(legacy_zip(tmp_path / "a"))
    b = fo_bhav.parse(udiff_zip(tmp_path / "b"))
    assert list(a.columns) == fo_bhav.COLUMNS == list(b.columns)
    shared = [c for c in fo_bhav.COLUMNS if c not in ("underlying", "lot", "expiry_actual")]
    pd.testing.assert_frame_equal(a[shared], b[shared])
    assert a["expiry_actual"].isna().all()
    assert (b["expiry_actual"] == b["expiry"]).all()


def test_legacy_frame_values(tmp_path):
    df = fo_bhav.parse(legacy_zip(tmp_path))
    fut = df[df.kind == "FUTIDX"].iloc[0]
    assert fut.date == pd.Timestamp("2024-01-02")
    assert fut.expiry == pd.Timestamp("2024-01-25")
    assert fut.strike == 0.0 and fut.right is None
    assert fut.oi == 500000 and fut.oi_change == 2500 and fut.contracts == 1000
    assert df.underlying.isna().all() and df.lot.isna().all()
    ce = df[(df.kind == "OPTIDX") & (df.right == "CE")].iloc[0]
    assert ce.strike == 21700.0 and ce.close == 210.5 and ce.oi_change == -500
    assert str(df.date.dtype) == "datetime64[ns]" and str(df.oi.dtype) == "int64"


def test_udiff_kinds_and_extras(tmp_path):
    df = fo_bhav.parse(udiff_zip(tmp_path))
    assert set(df.kind) == {"FUTIDX", "OPTIDX", "OPTSTK"}
    assert df[df.symbol == "NIFTY"].lot.eq(25).all()
    assert df.underlying.eq(21720.0).all()
    assert df[df.kind == "FUTIDX"].right.isna().all()


def test_unknown_format_raises(tmp_path):
    p = _zip(tmp_path / "x.csv.zip", "x.csv", "a,b\n1,2\n")
    with pytest.raises(DataError):
        fo_bhav.parse(p)


def test_status_and_sessions(tmp_path):
    legacy_zip(tmp_path, date(2024, 1, 2))
    udiff_zip(tmp_path, date(2024, 7, 8),
              rows=[r.replace("2024-01-02", "2024-07-08") for r in UDIFF_ROWS])
    marker = fo_bhav.raw_dir(tmp_path) / "2024" / "20240717.missing"
    marker.touch()
    assert fo_bhav.status(date(2024, 1, 2), tmp_path) == "file"
    assert fo_bhav.status(date(2024, 7, 17), tmp_path) == "missing"
    assert fo_bhav.status(date(2024, 7, 9), tmp_path) == "absent"
    assert fo_bhav.sessions(root=tmp_path) == [date(2024, 1, 2), date(2024, 7, 8)]
    assert fo_bhav.sessions(date(2024, 3, 1), None, tmp_path) == [date(2024, 7, 8)]
    assert fo_bhav.sessions(root=tmp_path / "nowhere") == []


def test_udiff_wins_when_both_forms_exist(tmp_path):
    legacy_zip(tmp_path)
    u = udiff_zip(tmp_path)
    assert fo_bhav.path_for(date(2024, 1, 2), tmp_path) == u


def test_load_filters_and_sorts(tmp_path):
    legacy_zip(tmp_path)
    df = fo_bhav.load(date(2024, 1, 2), symbols=["nifty"], kinds=["OPTIDX"], root=tmp_path)
    assert list(df.right) == ["CE", "PE"]
    assert (df.symbol == "NIFTY").all()


def test_load_without_file_raises(tmp_path):
    with pytest.raises(DataError, match="2024-01-03"):
        fo_bhav.load(date(2024, 1, 3), root=tmp_path)


def test_load_rejects_a_file_carrying_another_date(tmp_path):
    legacy_zip(tmp_path, date(2024, 1, 3))  # rows still say 02-JAN-2024
    with pytest.raises(DataError, match="2024-01-03"):
        fo_bhav.load(date(2024, 1, 3), root=tmp_path)


def test_load_range_concatenates_in_date_order(tmp_path):
    legacy_zip(tmp_path)
    udiff_zip(tmp_path, date(2024, 7, 8),
              rows=[r.replace("2024-01-02", "2024-07-08") for r in UDIFF_ROWS])
    df = fo_bhav.load_range(date(2024, 1, 1), date(2024, 12, 31),
                            symbols=fo_bhav.INDEX_SYMBOLS, root=tmp_path)
    assert list(df.date.dt.date.unique()) == [date(2024, 1, 2), date(2024, 7, 8)]
    assert "RELIANCE" not in set(df.symbol)
    empty = fo_bhav.load_range(date(2020, 1, 1), date(2020, 1, 5), root=tmp_path)
    assert list(empty.columns) == fo_bhav.COLUMNS and empty.empty
    assert empty.date.dtype == "datetime64[ns]" and empty.oi.dtype == "int64"
    assert dict(empty.dtypes) == dict(df.dtypes)


def test_garbled_date_names_the_file(tmp_path):
    p = legacy_zip(tmp_path, rows=[LEGACY_ROWS[0].replace("25-Jan-2024", "25-Jxx-2024")])
    with pytest.raises(DataError, match=re.escape(p.name)):
        fo_bhav.parse(p)


def test_garbled_price_raises(tmp_path):
    p = legacy_zip(tmp_path, rows=[LEGACY_ROWS[0].replace(",21700,21800,", ",abc,21800,")])
    with pytest.raises(DataError, match=re.escape(p.name)):
        fo_bhav.parse(p)


def test_header_without_rows_raises(tmp_path):
    p = udiff_zip(tmp_path, rows=[])
    with pytest.raises(DataError, match=re.escape(p.name)):
        fo_bhav.parse(p)


def test_load_kinds_ignore_case_and_bare_strings_are_rejected(tmp_path):
    legacy_zip(tmp_path)
    df = fo_bhav.load(date(2024, 1, 2), symbols=["NIFTY"], kinds=["optidx"], root=tmp_path)
    assert list(df.right) == ["CE", "PE"]
    with pytest.raises(TypeError):
        fo_bhav.load(date(2024, 1, 2), symbols="NIFTY", root=tmp_path)
    with pytest.raises(TypeError):
        fo_bhav.load(date(2024, 1, 2), kinds="OPTIDX", root=tmp_path)


def test_url_for_both_eras():
    from shunkan.data.nse_archive import ARCHIVE

    assert fo_bhav.url_for(date(2026, 11, 3)) == (
        ARCHIVE + "/content/fo/BhavCopy_NSE_FO_0_0_0_20261103_F_0000.csv.zip")
    assert fo_bhav.url_for(date(2024, 7, 8)).endswith("BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv.zip")
    assert fo_bhav.url_for(date(2024, 7, 5)) == (
        ARCHIVE + "/content/historical/DERIVATIVES/2024/JUL/fo05JUL2024bhav.csv.zip")


def _mock(status, body, seen):
    import httpx

    def handler(request):
        seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _zip_bytes(name, text):
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def _udiff_text(day):
    rows = [r.replace("2024-01-02", day) for r in UDIFF_ROWS]
    return "\n".join([",".join(UDIFF_COLS), *rows]) + "\n"


def test_fetch_returns_an_existing_file_without_the_network(tmp_path):
    p = legacy_zip(tmp_path)
    before = p.read_bytes()
    seen = []
    assert fo_bhav.fetch(date(2024, 1, 2), tmp_path, client=_mock(500, b"", seen)) == p
    assert seen == [] and p.read_bytes() == before


def test_fetch_saves_once_under_the_canonical_name(tmp_path):
    day = date(2026, 11, 3)
    name = "BhavCopy_NSE_FO_0_0_0_20261103_F_0000.csv"
    body = _zip_bytes(name, _udiff_text("2026-11-03"))
    seen = []
    p = fo_bhav.fetch(day, tmp_path, client=_mock(200, body, seen))
    assert p == fo_bhav.raw_dir(tmp_path) / "2026" / (name + ".zip")
    assert p.read_bytes() == body and fo_bhav.path_for(day, tmp_path) == p
    assert str(seen[0].url) == fo_bhav.url_for(day)
    assert fo_bhav.fetch(day, tmp_path, client=_mock(500, b"", seen)) == p and len(seen) == 1


@pytest.mark.parametrize("status,body,error", [
    (404, b"Not Found", "Unpublished"),
    (200, b"<HTML><TITLE>Access Denied</TITLE>", "FetchFailed")])
def test_fetch_failure_writes_nothing(tmp_path, status, body, error):
    from shunkan.data import provider

    with pytest.raises(getattr(provider, error)):
        fo_bhav.fetch(date(2026, 11, 3), tmp_path, client=_mock(status, body, []))
    assert not fo_bhav.raw_dir(tmp_path).exists()


@pytest.mark.parametrize("text", [_udiff_text("2026-11-02"), "a,b\n1,2\n"])
def test_fetch_never_caches_a_zip_that_is_not_the_days_bhavcopy(tmp_path, text):
    from shunkan.data.provider import FetchFailed

    name = "BhavCopy_NSE_FO_0_0_0_20261103_F_0000.csv"
    body = _zip_bytes(name, text)
    with pytest.raises(FetchFailed, match=re.escape(name + ".zip")):
        fo_bhav.fetch(date(2026, 11, 3), tmp_path, client=_mock(200, body, []))
    assert not fo_bhav.raw_dir(tmp_path).exists()

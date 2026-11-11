import os
import zipfile
from datetime import date

import pandas as pd
import pytest

from shunkan.data import fo_bhav, fo_store
from shunkan.data.provider import DataError

LEGACY_HEADER = ("INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,"
                 "SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,")
UDIFF_COLS = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
              "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
              "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty",
              "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]
UNI = {"OPTIDX": "IDO", "FUTIDX": "IDF", "OPTSTK": "STO", "FUTSTK": "STF"}

# deliberately unsorted, with both kinds, three symbols and a futures row without a right
CONTRACTS = [("OPTSTK", "RELIANCE", 2600, "PE"), ("OPTIDX", "NIFTY", 21800, "CE"),
             ("FUTIDX", "NIFTY", 0, ""), ("OPTIDX", "BANKNIFTY", 47000, "PE"),
             ("OPTIDX", "NIFTY", 21700, "PE"), ("OPTIDX", "NIFTY", 21700, "CE"),
             ("FUTSTK", "RELIANCE", 0, ""), ("OPTIDX", "BANKNIFTY", 47000, "CE")]


def _zip(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.name[:-4], text)
    return path


def day_zip(root, d, legacy=None, carry=None, extra=0):
    legacy = d < date(2024, 7, 8) if legacy is None else legacy
    on = carry or d
    exp = date(on.year, on.month, 25)
    rows = []
    for i, (kind, sym, strike, right) in enumerate(CONTRACTS):
        px = 100 + i + d.day + extra
        if legacy:
            rows.append(f"{kind},{sym},{exp:%d-%b-%Y},{strike},{right or 'XX'},{px},{px + 5},{px - 5},"
                        f"{px + 1},{px + 1},{i * 3},1,{1000 + i},{i - 3},{on:%d-%b-%Y}".upper() + ",")
        else:
            v = dict.fromkeys(UDIFF_COLS, "")
            v.update(TradDt=f"{on}", BizDt=f"{on}", Sgmt="FO", Src="NSE", FinInstrmTp=UNI[kind],
                     TckrSymb=sym, XpryDt=f"{exp}", StrkPric=f"{strike}.00" if strike else "",
                     OptnTp=right, OpnPric=f"{px}", HghPric=f"{px + 5}", LwPric=f"{px - 5}",
                     ClsPric=f"{px + 1}", SttlmPric=f"{px + 1}", OpnIntrst=f"{1000 + i}",
                     ChngInOpnIntrst=f"{i - 3}", TtlTradgVol=f"{i * 3}", UndrlygPric="21720.5",
                     NewBrdLotQty="25" if "NIFTY" in sym else "250", SsnId="F1")
            rows.append(",".join(v[k] for k in UDIFF_COLS))
    folder = fo_bhav.raw_dir(root) / f"{d:%Y}"
    if legacy:
        p = folder / f"fo{d:%d}{d:%b}{d:%Y}bhav.csv.zip".replace(f"{d:%b}", f"{d:%b}".upper())
        return _zip(p, "\n".join([LEGACY_HEADER, *rows]) + "\n")
    p = folder / f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    return _zip(p, "\n".join([",".join(UDIFF_COLS), *rows]) + "\n")


DAYS = [date(2024, 6, 27), date(2024, 6, 28), date(2024, 7, 4), date(2024, 7, 5),
        date(2024, 7, 8), date(2024, 7, 9), date(2024, 8, 1)]
FILTERS = [(None, None), (["NIFTY"], None), (None, ["OPTIDX"]),
           (["nifty", "BANKNIFTY"], ["OPTIDX", "futidx"]), (["NOSUCH"], None)]


@pytest.fixture
def store(tmp_path):
    for d in DAYS:
        day_zip(tmp_path, d)
    return tmp_path


def raw_only(fn, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(fo_store, "_manifest", lambda root: {})
        return fn()


def no_raw(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("raw parse while the store holds the day")
    monkeypatch.setattr(fo_bhav, "parse", boom)


def test_store_reproduces_the_raw_loader_for_every_filter(store, monkeypatch):
    spans = [(date(2024, 1, 1), date(2024, 12, 31)), (date(2024, 6, 28), date(2024, 7, 8))]
    want = {(s, k, span): raw_only(lambda: fo_bhav.load_range(*span, symbols=s, kinds=k, root=store),
                                   monkeypatch)
            for s, k in [(tuple(a) if a else None, tuple(b) if b else None) for a, b in FILTERS]
            for span in spans}
    days = {(d, s, k): raw_only(lambda: fo_bhav.load(d, s and list(s), k and list(k), store), monkeypatch)
            for d in DAYS for s, k in [(tuple(a) if a else None, tuple(b) if b else None) for a, b in FILTERS]}
    res = fo_store.build(store, workers=1)
    assert (res["months"], res["days"], res["failed"], res["built"]) == (3, 7, {}, 3)
    no_raw(monkeypatch)
    for (s, k, span), exp in want.items():
        got = fo_bhav.load_range(*span, symbols=s and list(s), kinds=k and list(k), root=store)
        pd.testing.assert_frame_equal(got, exp)
        assert dict(got.dtypes) == dict(exp.dtypes)
    for (d, s, k), exp in days.items():
        pd.testing.assert_frame_equal(fo_bhav.load(d, s and list(s), k and list(k), store), exp)
    fut = fo_bhav.load(date(2024, 7, 8), ["NIFTY"], ["FUTIDX"], store)
    assert fut["right"].tolist() == [None] and fut["right"].dtype == object


def test_rebuild_is_a_no_op_and_parallel_build_matches(store, tmp_path_factory, monkeypatch):
    fo_store.build(store, workers=1)
    assert fo_store.build(store, workers=1)["built"] == 0
    other = tmp_path_factory.mktemp("par")
    for d in DAYS:
        day_zip(other, d)
    assert fo_store.build(other, workers=2)["built"] == 3
    no_raw(monkeypatch)
    span = (date(2024, 1, 1), date(2024, 12, 31))
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), fo_bhav.load_range(*span, root=other))


def test_days_newer_than_the_store_come_from_raw_until_the_update(store, monkeypatch):
    fo_store.build(store, workers=1)
    day_zip(store, date(2024, 8, 2))
    day_zip(store, date(2024, 9, 2))
    span = (date(2024, 1, 1), date(2024, 12, 31))
    exp = raw_only(lambda: fo_bhav.load_range(*span, root=store), monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), exp)
    res = fo_store.build(store, workers=1, create=False)
    assert (res["built"], res["days"], res["months"]) == (2, 9, 4)
    no_raw(monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), exp)


@pytest.mark.parametrize("day", [date(2024, 7, 10), date(2024, 8, 2), date(2025, 1, 2)])
def test_a_day_without_a_file_still_raises(store, day):
    fo_store.build(store, workers=1)
    with pytest.raises(DataError, match=f"no F&O bhavcopy on disk for {day}"):
        fo_bhav.load(day, root=store)
    got, rest = fo_store.read([day], root=store)
    assert got is None and rest == [day]


def test_update_never_creates_a_store(store):
    assert "skipped" in fo_store.build(store, create=False)
    assert not (fo_store.typed_dir(store) / "manifest.json").exists()


def test_a_replaced_raw_file_is_not_trusted(store, monkeypatch):
    fo_store.build(store, workers=1)
    d = date(2024, 7, 9)
    day_zip(store, d, extra=1000)
    exp = raw_only(lambda: fo_bhav.load(d, root=store), monkeypatch)
    assert exp["open"].min() > 1000
    pd.testing.assert_frame_equal(fo_bhav.load(d, root=store), exp)
    assert fo_store.build(store, workers=1)["built"] == 1
    no_raw(monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load(d, root=store), exp)


def test_a_touched_raw_file_is_reread(store, monkeypatch):
    fo_store.build(store, workers=1)
    p = fo_bhav.path_for(date(2024, 6, 27), store)
    os.utime(p, ns=(1, 1))
    _, rest = fo_store.read([date(2024, 6, 27), date(2024, 6, 28)], root=store)
    assert rest == [date(2024, 6, 27)]


def test_a_bad_day_is_left_out_and_still_raises(store, monkeypatch):
    bad = date(2024, 7, 10)
    day_zip(store, bad, carry=date(2024, 7, 9))
    span = (date(2024, 7, 1), date(2024, 7, 31))
    with pytest.raises(DataError) as before:
        raw_only(lambda: fo_bhav.load_range(*span, root=store), monkeypatch)
    res = fo_store.build(store, workers=1)
    assert list(res["failed"]) == ["2024-07-10"] and res["days"] == 7
    with pytest.raises(DataError) as after:
        fo_bhav.load_range(*span, root=store)
    assert str(after.value) == str(before.value)
    assert fo_store.build(store, workers=1)["built"] == 0


def test_a_new_format_retires_the_store(store, monkeypatch):
    fo_store.build(store, workers=1)
    monkeypatch.setattr(fo_store, "format_hash", lambda: "another-parser")
    got, rest = fo_store.read(DAYS, root=store)
    assert got is None and rest == DAYS
    assert "skipped" in fo_store.build(store, create=False)
    assert fo_store.build(store, workers=1)["built"] == 3


@pytest.mark.parametrize("damage", ["manifest", "missing", "garbled"])
def test_a_damaged_store_falls_back_to_raw(store, monkeypatch, damage):
    span = (date(2024, 1, 1), date(2024, 12, 31))
    exp = raw_only(lambda: fo_bhav.load_range(*span, root=store), monkeypatch)
    fo_store.build(store, workers=1)
    base = fo_store.typed_dir(store)
    july = base / "2024" / "2024-07.parquet"
    if damage == "manifest":
        (base / "manifest.json").write_text("{not json")
    elif damage == "missing":
        july.unlink()
    else:
        july.write_bytes(b"PAR1 not really")
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), exp)
    assert fo_store.build(store, workers=1)["built"] >= 1
    no_raw(monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), exp)


def test_a_month_without_raw_files_leaves_the_store(store):
    fo_store.build(store, workers=1)
    fo_bhav.path_for(date(2024, 8, 1), store).unlink()
    res = fo_store.build(store, workers=1)
    assert (res["months"], res["days"]) == (2, 6)
    assert not (fo_store.typed_dir(store) / "2024" / "2024-08.parquet").exists()


def test_a_second_builder_is_turned_away(store):
    with fo_store._lock(fo_store.typed_dir(store)) as ok:
        assert ok
        assert fo_store.build(store, workers=1) == {"busy": True}


def test_refresh_updates_an_existing_store(monkeypatch, tmp_path):
    from shunkan.analytics import facts

    calls = []
    monkeypatch.setattr(fo_store, "build", lambda *a, **k: calls.append(k) or {})
    monkeypatch.setattr(fo_bhav, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(fo_bhav, "path_for", lambda *a, **k: "x")
    for name in ("index_bars", "participant", "flows"):
        mod = getattr(facts, name)
        for fn in ("fetch", "day_table", "fii_derivatives"):
            if hasattr(mod, fn):
                monkeypatch.setattr(mod, fn, lambda *a, **k: None)
    monkeypatch.setattr(facts, "_cash_for", lambda *a, **k: None)
    monkeypatch.setattr(facts.cal, "_holidays", lambda *a, **k: ([], None))
    monkeypatch.setattr(facts.cal, "board_meetings", lambda *a, **k: None)
    monkeypatch.setattr(facts.cal, "index_universe", lambda *a, **k: None)
    facts.refresh(root=tmp_path)
    assert calls == [{"workers": 1, "create": False}]


def test_store_frames_take_in_place_edits(store):
    fo_store.build(store, workers=1)
    for df in (fo_bhav.load(date(2024, 7, 8), root=store),
               fo_bhav.load_range(date(2024, 7, 1), date(2024, 7, 31), ["NIFTY"], ["OPTIDX"], store)):
        df.loc[0, "close"] = 1.0
        df.iloc[0, df.columns.get_loc("oi")] = 7
        assert df.loc[0, "close"] == 1.0 and df.loc[0, "oi"] == 7


@pytest.mark.parametrize("stale", ["file", "manifest"])
def test_a_month_file_the_manifest_does_not_describe_is_not_served(store, monkeypatch, stale):
    base = fo_store.typed_dir(store)
    fo_store.build(store, workers=1)
    july, man = base / "2024" / "2024-07.parquet", base / "manifest.json"
    old_file, old_man = july.read_bytes(), man.read_bytes()
    day_zip(store, date(2024, 7, 10))
    fo_store.build(store, workers=1)
    (july if stale == "file" else man).write_bytes(old_file if stale == "file" else old_man)
    span = (date(2024, 7, 1), date(2024, 7, 31))
    exp = raw_only(lambda: fo_bhav.load_range(*span, root=store), monkeypatch)
    assert len(exp) == 5 * len(CONTRACTS)
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), exp)
    assert fo_store.build(store, workers=1)["built"] == 1
    no_raw(monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=store), exp)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file")
def test_an_unreadable_raw_file_fails_the_build_and_keeps_the_other_months(store, monkeypatch):
    p = fo_bhav.path_for(date(2024, 7, 9), store)
    p.chmod(0)
    try:
        with pytest.raises(DataError):
            fo_store.build(store, workers=2)
        res = fo_store.summary(store)
        assert (res["months"], res["failed"]) == (2, {})
    finally:
        p.chmod(0o644)
    assert fo_store.build(store, workers=1)["built"] == 1
    no_raw(monkeypatch)
    assert len(fo_bhav.load(date(2024, 7, 9), root=store)) == len(CONTRACTS)


def test_the_evening_update_never_finishes_a_full_build(store):
    fo_store.build(store, workers=1)
    for d in (date(2024, 6, 27), date(2024, 7, 4), date(2024, 8, 1)):
        os.utime(fo_bhav.path_for(d, store), ns=(1, 1))
    res = fo_store.build(store, workers=1, create=False)
    assert "3 months out of date" in res["skipped"]
    assert fo_store.build(store, workers=1)["built"] == 3


def test_the_format_hash_is_the_running_code_and_survives_hash_seeds(monkeypatch):
    import subprocess
    import sys

    code = "from shunkan.data import fo_store; print(fo_store.format_hash())"
    seeds = {subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": s},
                            capture_output=True, text=True, check=True).stdout.strip() for s in ("1", "2", "3")}
    assert seeds == {fo_store.format_hash()}
    monkeypatch.setattr(pd, "__version__", "0.0.0")
    assert fo_store._format() != fo_store.format_hash()


def test_a_month_without_any_option_rows_round_trips(tmp_path, monkeypatch):
    global CONTRACTS
    keep = CONTRACTS
    for d in (date(2024, 6, 27), date(2024, 7, 8)):
        day_zip(tmp_path, d)
    CONTRACTS = [c for c in keep if not c[3]]
    try:
        day_zip(tmp_path, date(2024, 8, 1))
    finally:
        CONTRACTS = keep
    span = (date(2024, 1, 1), date(2024, 12, 31))
    exp = raw_only(lambda: fo_bhav.load_range(*span, root=tmp_path), monkeypatch)
    fo_store.build(tmp_path, workers=1)
    no_raw(monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load_range(*span, root=tmp_path), exp)


def test_a_malformed_manifest_entry_is_rebuilt(store, monkeypatch):
    import json

    fo_store.build(store, workers=1)
    man = fo_store.typed_dir(store) / "manifest.json"
    m = json.loads(man.read_text())
    m["months"]["2024-07"]["failed"] = 5
    man.write_text(json.dumps(m))
    exp = raw_only(lambda: fo_bhav.load(date(2024, 7, 8), root=store), monkeypatch)
    pd.testing.assert_frame_equal(fo_bhav.load(date(2024, 7, 8), root=store), exp)
    assert fo_store.build(store, workers=1)["built"] == 1


def test_a_malformed_raw_name_does_not_break_single_day_loads(store):
    fo_store.build(store, workers=1)
    (fo_bhav.raw_dir(store) / "2024" / "BhavCopy_NSE_FO_0_0_0_20240230_F_0000.csv.zip").write_bytes(b"x")
    assert len(fo_bhav.load(date(2024, 7, 8), root=store)) == len(CONTRACTS)

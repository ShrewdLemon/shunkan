import math
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

from shunkan.data import fo_bhav, fo_iv, fo_store, rates
from shunkan.derivatives import black76

R = 0.065
UDIFF_COLS = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
              "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
              "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty",
              "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]
UNI = {"OPTIDX": "IDO", "FUTIDX": "IDF", "OPTSTK": "STO", "FUTSTK": "STF"}


@pytest.fixture(autouse=True)
def flat_rate(monkeypatch):
    monkeypatch.setattr(rates, "table", lambda: (np.array(["2018-08-01"], dtype="datetime64[D]"),
                                                 np.array([R])))


def smile(F, K):
    x = math.log(K / F)
    return 0.13 - 0.4 * x + 1.5 * x * x


def chain(day, expiry, F, strikes, symbol="NIFTY", kind="OPTIDX", untraded=(), skip=(),
          vol=smile, bump=0.0):
    t = (expiry - day).days / 365
    DF = math.exp(-R * t)
    out = []
    for k in strikes:
        for right in ("CE", "PE"):
            if (k, right) in skip:
                continue
            if t > 0:
                px = float(black76.price(F, k, t, DF, vol(F, k), right == "CE"))
            else:
                px = (max(F - k, 0.0) if right == "CE" else max(k - F, 0.0)) + 2.0
            out.append(dict(date=day, kind=kind, symbol=symbol, expiry=expiry, strike=float(k),
                            right=right, close=px + bump,
                            contracts=0 if (k, right) in untraded else 10))
    return out


def future(day, expiry, close, symbol="NIFTY", kind="FUTIDX"):
    return [dict(date=day, kind=kind, symbol=symbol, expiry=expiry, strike=np.nan, right=None,
                 close=close, contracts=100)]


def frame(rows):
    full = []
    for r in rows:
        v = dict(open=r["close"], high=r["close"], low=r["close"], settle=r["close"], oi=1000,
                 oi_change=0, underlying=np.nan, lot=65.0, expiry_actual=r["expiry"])
        v.update(r)
        for c in ("date", "expiry", "expiry_actual"):
            v[c] = pd.Timestamp(v[c])
        full.append(v)
    return pd.DataFrame(full)[fo_bhav.COLUMNS].astype(fo_bhav._DTYPES)


D = date(2024, 8, 1)
WEEK = date(2024, 8, 8)
MONTH = date(2024, 8, 29)
STRIKES = range(24000, 26100, 100)


def test_forward_and_iv_come_back_exactly_from_parity():
    rows = chain(D, WEEK, 25080.0, STRIKES) + chain(D, MONTH, 25140.0, STRIKES)
    rows += future(D, MONTH, 25155.0)
    out = fo_iv.compute(frame(rows))
    assert list(out.columns) == fo_iv.COLUMNS
    for exp, F in ((WEEK, 25080.0), (MONTH, 25140.0)):
        g = out[out["expiry"] == pd.Timestamp(exp)]
        assert g["forward"].to_numpy() == pytest.approx(F, abs=1e-8)
        assert set(g["fwd_src"]) == {fo_iv.FWD_PARITY} and set(g["pairs"]) == {3}
        assert (g["code"] == black76.OK).all()
        want = [smile(F, k) for k in g["strike"]]
        assert g["iv"].to_numpy() == pytest.approx(want, abs=1e-9)
        t = (exp - D).days / 365
        assert g["t"].to_numpy() == pytest.approx(t) and g["df"].to_numpy() == pytest.approx(math.exp(-R * t))


def test_one_bad_pair_cannot_move_the_forward():
    rows = chain(D, WEEK, 25080.0, STRIKES)
    for r in rows:
        if r["strike"] == 25100.0 and r["right"] == "CE":
            r["close"] += 40.0
    out = fo_iv.compute(frame(rows))
    assert out["forward"].iloc[0] == pytest.approx(25080.0, abs=1e-8)


def test_no_pair_falls_back_to_the_future_and_then_to_nothing():
    puts_gone = {(k, "PE") for k in STRIKES}
    rows = chain(D, MONTH, 25140.0, STRIKES, skip=puts_gone) + future(D, MONTH, 25140.0)
    rows += chain(D, WEEK, 25080.0, STRIKES, skip=puts_gone)
    out = fo_iv.compute(frame(rows))
    m = out[out["expiry"] == pd.Timestamp(MONTH)]
    assert set(m["fwd_src"]) == {fo_iv.FWD_FUTURE} and set(m["pairs"]) == {0}
    assert m["iv"].to_numpy() == pytest.approx([smile(25140.0, k) for k in m["strike"]], abs=1e-9)
    w = out[out["expiry"] == pd.Timestamp(WEEK)]
    assert set(w["fwd_src"]) == {fo_iv.FWD_NONE}
    assert set(w["code"]) == {black76.NO_FORWARD} and w["iv"].isna().all()


def test_one_pair_loses_to_a_traded_future_but_beats_nothing():
    one = {(k, "PE") for k in STRIKES if k != 25100}
    rows = chain(D, MONTH, 25140.0, STRIKES, skip=one) + future(D, MONTH, 25150.0)
    rows += chain(D, WEEK, 25080.0, STRIKES, skip=one)
    out = fo_iv.compute(frame(rows)).groupby("expiry")[["forward", "fwd_src", "pairs"]].first()
    assert out.loc[pd.Timestamp(MONTH)].tolist() == [25150.0, fo_iv.FWD_FUTURE, 0]
    assert out.loc[pd.Timestamp(WEEK), "forward"] == pytest.approx(25080.0)
    assert out.loc[pd.Timestamp(WEEK), ["fwd_src", "pairs"]].tolist() == [fo_iv.FWD_PARITY, 1]


def test_untraded_rows_get_no_row_and_expiry_day_gets_no_iv():
    rows = chain(D, WEEK, 25080.0, STRIKES, untraded={(25000, "CE"), (25000, "PE")})
    rows += chain(D, D, 25060.0, range(24900, 25300, 100))
    out = fo_iv.compute(frame(rows))
    assert not ((out["strike"] == 25000.0) & (out["expiry"] == pd.Timestamp(WEEK))).any()
    today = out[out["expiry"] == pd.Timestamp(D)]
    assert len(today) == 8 and set(today["code"]) == {black76.EXPIRED}
    assert today["forward"].iloc[0] == pytest.approx(25060.0)


def test_a_print_below_intrinsic_is_flagged_not_solved():
    rows = chain(D, WEEK, 25080.0, STRIKES)
    for r in rows:
        if r["strike"] == 24000.0 and r["right"] == "CE":
            r["close"] = 1000.0
    out = fo_iv.compute(frame(rows))
    bad = out[(out["strike"] == 24000.0) & (out["right"] == "CE")]
    assert bad["code"].item() == black76.BELOW_INTRINSIC and np.isnan(bad["iv"].item())


def test_an_early_actual_expiry_sets_t_and_ends_the_contract_that_day():
    rows = chain(D, MONTH, 2950.0, range(2700, 3250, 50), symbol="TATAMOTORS", kind="OPTSTK")
    rows += chain(D, MONTH, 25140.0, STRIKES)
    early = date(2024, 8, 6)
    for r in rows:
        if r["symbol"] == "TATAMOTORS":
            r["expiry_actual"] = early
    out = fo_iv.compute(frame(rows))
    tm = out[out["symbol"] == "TATAMOTORS"]
    assert tm["t"].to_numpy() == pytest.approx((early - D).days / 365)
    assert (out[out["symbol"] == "NIFTY"]["t"] == (MONTH - D).days / 365).all()
    for r in rows:
        if r["symbol"] == "TATAMOTORS":
            r["expiry_actual"] = D
    tm = fo_iv.compute(frame(rows)).query("symbol == 'TATAMOTORS'")
    assert set(tm["code"]) == {black76.EXPIRED}


def test_a_legacy_row_without_an_actual_expiry_uses_the_contract_expiry():
    rows = chain(D, MONTH, 25140.0, STRIKES)
    for r in rows:
        r["expiry_actual"] = None
    out = fo_iv.compute(frame(rows))
    assert (out["t"] == (MONTH - D).days / 365).all() and (out["code"] == black76.OK).all()


def test_stock_options_use_their_own_parity():
    rows = chain(D, MONTH, 2950.0, range(2700, 3250, 50), symbol="RELIANCE", kind="OPTSTK")
    rows += future(D, MONTH, 2951.0, "RELIANCE", "FUTSTK") + chain(D, MONTH, 25140.0, STRIKES)
    out = fo_iv.compute(frame(rows))
    rel = out[out["symbol"] == "RELIANCE"]
    assert rel["forward"].to_numpy() == pytest.approx(2950.0, abs=1e-8)
    assert list(out["kind"].unique()) == ["OPTIDX", "OPTSTK"]


def test_greeks_on_read_and_nan_where_there_is_no_iv():
    rows = chain(D, WEEK, 25080.0, STRIKES) + chain(D, D, 25060.0, [25000])
    g = fo_iv.with_greeks(fo_iv.compute(frame(rows)))
    ok = g["code"] == black76.OK
    assert g.loc[ok, fo_iv.GREEKS].notna().all().all() and g.loc[~ok, fo_iv.GREEKS].isna().all().all()
    atm = g[(g["strike"] == 25100.0) & (g["expiry"] == pd.Timestamp(WEEK))].set_index("right")
    assert atm.loc["CE", "delta"] - atm.loc["PE", "delta"] == pytest.approx(1.0)


# --- the store -----------------------------------------------------------------

def write_day(root, d, rows):
    lines = []
    for r in rows:
        v = dict.fromkeys(UDIFF_COLS, "")
        strike = r["strike"]
        v.update(TradDt=f"{d}", BizDt=f"{d}", Sgmt="FO", Src="NSE", FinInstrmTp=UNI[r["kind"]],
                 TckrSymb=r["symbol"], XpryDt=f"{r['expiry']}",
                 StrkPric="" if strike != strike else f"{strike:.2f}", OptnTp=r["right"] or "",
                 OpnPric=f"{r['close']:.10f}", HghPric=f"{r['close']:.10f}",
                 LwPric=f"{r['close']:.10f}", ClsPric=f"{r['close']:.10f}",
                 SttlmPric=f"{r['close']:.10f}", OpnIntrst="1000", ChngInOpnIntrst="0",
                 TtlTradgVol=str(r["contracts"]), UndrlygPric="25000", NewBrdLotQty="25", SsnId="F1")
        lines.append(",".join(v[k] for k in UDIFF_COLS))
    p = fo_bhav.raw_dir(root) / f"{d:%Y}" / f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(p.name[:-4], "\n".join([",".join(UDIFF_COLS), *lines]) + "\n")
    return p


DAYS = [date(2024, 8, 1), date(2024, 8, 2), date(2024, 9, 2), date(2024, 9, 3)]


def day_rows(d, bump=0.0):
    exp = date(2024, 8, 29) if d.month == 8 else date(2024, 9, 26)
    week = date(d.year, d.month, d.day + 6)
    f = 25000.0 + 10 * d.day
    return (chain(d, exp, f + 60, STRIKES, bump=bump) + future(d, exp, f + 61)
            + chain(d, week, f, STRIKES, bump=bump)
            + chain(d, exp, 2950.0, range(2800, 3150, 50), "RELIANCE", "OPTSTK")
            + future(d, exp, 2951.0, "RELIANCE", "FUTSTK"))


@pytest.fixture
def store(tmp_path):
    for d in DAYS:
        write_day(tmp_path, d, day_rows(d))
    return tmp_path


def computed(root, symbols=None, kinds=None, monkeypatch=None):
    with monkeypatch.context() as m:
        m.setattr(fo_store, "_manifest", lambda root: {})
        m.setattr(fo_iv, "_manifest", lambda root: {})
        return fo_iv.load_range(DAYS[0], DAYS[-1], symbols, kinds, root)


FILTERS = [(None, None), (["NIFTY"], None), (None, ["OPTSTK"]), (["reliance"], ["optstk"])]


@pytest.mark.parametrize("symbols,kinds", FILTERS)
def test_stored_rows_equal_computed_rows(store, monkeypatch, symbols, kinds):
    fo_store.build(store, workers=1)
    out = fo_iv.build(store, workers=1)
    assert out["months"] == 2 and out["days"] == 4 and out["built"] == 2
    got = fo_iv.load_range(DAYS[0], DAYS[-1], symbols, kinds, store)
    pd.testing.assert_frame_equal(got, computed(store, symbols, kinds, monkeypatch))
    assert len(got) and list(got.columns) == fo_iv.COLUMNS + fo_iv.GREEKS


def test_load_order_is_date_then_kind_symbol_expiry_strike_right(store):
    fo_store.build(store, workers=1)
    fo_iv.build(store, workers=1)
    got = fo_iv.load_range(DAYS[0], DAYS[-1], root=store, greeks=False)
    keys = ["date", "kind", "symbol", "expiry", "strike", "right"]
    pd.testing.assert_frame_equal(got, got.sort_values(keys, kind="stable", ignore_index=True))


def test_a_changed_raw_day_is_computed_until_both_stores_catch_up(store, monkeypatch):
    fo_store.build(store, workers=1)
    fo_iv.build(store, workers=1)
    write_day(store, DAYS[2], day_rows(DAYS[2], bump=0.5))
    fresh = computed(store, None, None, monkeypatch)
    pd.testing.assert_frame_equal(fo_iv.load_range(DAYS[0], DAYS[-1], root=store), fresh)
    assert fo_store.build(store, workers=1)["built"] == 1
    hit, miss = fo_iv._valid(DAYS, store)
    assert sorted(hit) == ["2024-08"] and miss == DAYS[2:]
    pd.testing.assert_frame_equal(fo_iv.load_range(DAYS[0], DAYS[-1], root=store), fresh)
    assert fo_iv.build(store, workers=1)["built"] == 1
    assert fo_iv._valid(DAYS, store)[1] == []
    pd.testing.assert_frame_equal(fo_iv.load_range(DAYS[0], DAYS[-1], root=store), fresh)


def test_a_new_rate_retires_only_the_months_it_touches(store, monkeypatch):
    fo_store.build(store, workers=1)
    fo_iv.build(store, workers=1)
    monkeypatch.setattr(rates, "table", lambda: (
        np.array(["2018-08-01", "2024-09-03"], dtype="datetime64[D]"), np.array([R, 0.06])))
    hit, miss = fo_iv._valid(DAYS, store)
    assert sorted(hit) == ["2024-08"] and miss == DAYS[2:]
    want = computed(store, None, None, monkeypatch)
    pd.testing.assert_frame_equal(fo_iv.load_range(DAYS[0], DAYS[-1], root=store), want)
    assert fo_iv.build(store, workers=1)["built"] == 1
    sep3 = want[want["date"] == pd.Timestamp(DAYS[3])]
    assert sep3["df"].to_numpy() == pytest.approx(np.exp(-0.06 * sep3["t"].to_numpy()))


def test_a_garbled_month_file_is_computed_and_rebuilt(store, monkeypatch):
    fo_store.build(store, workers=1)
    fo_iv.build(store, workers=1)
    path = fo_store._month_file(fo_iv.iv_dir(store), "2024-09")
    path.write_bytes(b"PAR1 not parquet")
    pd.testing.assert_frame_equal(fo_iv.load_range(DAYS[0], DAYS[-1], root=store),
                                  computed(store, None, None, monkeypatch))
    assert fo_iv.build(store, workers=1)["built"] == 1


def test_evening_update_never_starts_a_store_and_caps_its_work(store, monkeypatch):
    fo_store.build(store, workers=1)
    assert "skipped" in fo_iv.build(store, create=False)
    fo_iv.build(store, workers=1)
    monkeypatch.setattr(fo_iv, "UPDATE_MAX", 1)
    monkeypatch.setattr(rates, "table", lambda: (np.array(["2018-08-01"], dtype="datetime64[D]"),
                                                 np.array([0.05])))
    assert "skipped" in fo_iv.build(store, create=False)
    assert fo_iv.build(store, workers=1)["built"] == 2


def test_an_unreadable_typed_store_never_deletes_iv_months(store, monkeypatch):
    fo_store.build(store, workers=1)
    fo_iv.build(store, workers=1)
    files = sorted(fo_iv.iv_dir(store).glob("*/*.parquet"))
    monkeypatch.setattr(fo_store, "_manifest", lambda root: {})
    for create in (False, True):
        assert "skipped" in fo_iv.build(store, workers=1, create=create)
    monkeypatch.undo()
    assert sorted(fo_iv.iv_dir(store).glob("*/*.parquet")) == files
    assert fo_iv.build(store, workers=1, create=False)["built"] == 0


def test_a_month_with_no_traded_option_is_stored_as_empty_once(tmp_path, monkeypatch):
    for d in DAYS[:2]:
        rows = day_rows(d)
        for r in rows:
            if r["kind"] in fo_iv.OPTIONS:
                r["contracts"] = 0
        write_day(tmp_path, d, rows)
    fo_store.build(tmp_path, workers=1)
    assert fo_iv.build(tmp_path, workers=1)["built"] == 1
    assert fo_iv.build(tmp_path, workers=1)["built"] == 0
    assert not list(fo_iv.iv_dir(tmp_path).glob("*/*.parquet"))
    assert fo_iv._valid(DAYS[:2], tmp_path) == ({}, [])
    assert fo_iv.load_range(DAYS[0], DAYS[1], root=tmp_path).empty


def test_format_ignores_docstrings_but_sees_constants_and_defaults(monkeypatch):
    def a(x):
        """one"""
        return x + 1

    def b(x):
        """two, longer"""
        return x + 1

    assert fo_store._canon(a.__code__, a.__doc__) == fo_store._canon(b.__code__, b.__doc__)
    base = fo_iv._format()
    monkeypatch.setattr(black76.implied_vol, "__defaults__", (5,))
    assert fo_iv._format() != base
    monkeypatch.undo()
    monkeypatch.setattr(black76, "_C", tuple(c * 1.01 for c in black76._C))
    assert fo_iv._format() != base
    monkeypatch.undo()
    monkeypatch.setattr(fo_iv, "FWD_FUTURE", 7)
    assert fo_iv._format() != base


def test_a_held_lock_reports_busy(store):
    fo_store.build(store, workers=1)
    with fo_store._lock(fo_iv.iv_dir(store)):
        assert fo_iv.build(store, workers=1) == {"busy": True}


def test_only_option_kinds_have_iv_rows(store):
    with pytest.raises(ValueError, match="OPTIDX"):
        fo_iv.load_range(DAYS[0], DAYS[-1], kinds=["FUTIDX"], root=store)


def test_summary_counts_codes(store):
    fo_store.build(store, workers=1)
    s = fo_iv.build(store, workers=1)
    assert s["codes"] == {"ok": s["rows"]} and s["bytes"] > 0

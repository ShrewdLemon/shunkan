import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
import pcr_daily as pc  # noqa: E402
import vrp_condor as vc  # noqa: E402
from shunkan.backtest.validate import holm, newey_west_t  # noqa: E402


def test_pcr_near_skips_todays_expiry():
    rows = []
    d = pd.Timestamp("2025-01-09")
    for exp, put, call in [("2025-01-09", 900, 100), ("2025-01-16", 300, 200), ("2025-01-30", 50, 50)]:
        rows.append(dict(date=d, expiry=pd.Timestamp(exp), right="PE", oi=put))
        rows.append(dict(date=d, expiry=pd.Timestamp(exp), right="CE", oi=call))
    p = pc.pcr_series(pd.DataFrame(rows))
    assert p.loc[d, "near"] == pytest.approx(1.5)
    assert p.loc[d, "all"] == pytest.approx(1250 / 350)


def test_outcomes_trade_from_next_open():
    dates = pd.bdate_range("2025-01-01", periods=10)
    spot = pd.DataFrame({"date": dates, "open": np.arange(100.0, 110.0),
                         "close": np.arange(100.5, 110.5)})
    o = pc.outcomes(spot, dates)
    assert o["r1"].iloc[0] == pytest.approx(np.log(102.0 / 101.0))
    assert o["r5"].iloc[0] == pytest.approx(np.log(106.0 / 101.0))
    assert np.isnan(o["r5"].iloc[-6])


def test_ic_test_finds_a_planted_signal_and_rejects_noise():
    rng = np.random.default_rng(3)
    s = pd.Series(rng.normal(size=1500))
    y = 0.3 * s + rng.normal(size=1500)
    ic, t = pc.ic_test(s, y, lags=1)
    assert ic > 0.2 and t > 5
    ic0, t0 = pc.ic_test(s, pd.Series(rng.normal(size=1500)), lags=1)
    assert abs(t0) < 3


def test_eight_tests_with_holm():
    rng = np.random.default_rng(4)
    n = 600
    idx = pd.bdate_range("2020-01-01", periods=n)
    sig = pd.DataFrame({f"{d}_{t}": rng.normal(size=n) for d in ("near", "all") for t in ("z60", "d1")},
                       index=idx)
    out = pd.DataFrame({"r1": rng.normal(size=n), "r5": rng.normal(size=n),
                        "past1": rng.normal(size=n), "past5": rng.normal(size=n)}, index=idx)
    res = pc.run_tests(sig, out)
    assert len(res) == 8
    assert (res["p_holm"] >= res["p"] - 1e-12).all()
    assert not res["survives"].any()


# --- definitions ---------------------------------------------------------------

def oi_row(day, expiry, right, oi, symbol="NIFTY", kind="OPTIDX"):
    return {"date": pd.Timestamp(day), "kind": kind, "symbol": symbol,
            "expiry": pd.Timestamp(expiry), "strike": 24000.0 if right else np.nan,
            "right": right, "oi": np.int64(oi)}


def test_pcr_ignores_futures_and_other_symbols_and_leaves_undefined_ratios_nan():
    rows = [oi_row("2025-01-02", "2025-01-09", "PE", 300), oi_row("2025-01-02", "2025-01-09", "CE", 200),
            oi_row("2025-01-02", "2025-01-30", None, 10**9, kind="FUTIDX"),
            oi_row("2025-01-02", "2025-01-09", "PE", 10**9, symbol="BANKNIFTY"),
            oi_row("2025-01-09", "2025-01-09", "PE", 80), oi_row("2025-01-09", "2025-01-09", "CE", 40),
            oi_row("2025-01-10", "2025-01-16", "PE", 50), oi_row("2025-01-10", "2025-01-16", "CE", 0)]
    df = pd.DataFrame(rows).astype({"right": object, "oi": "int64"})
    p = pc.pcr_series(df)
    assert list(p.columns) == ["near", "all"]
    assert list(p.index) == [pd.Timestamp(x) for x in ("2025-01-02", "2025-01-09", "2025-01-10")]
    assert p.loc["2025-01-02", "near"] == pytest.approx(1.5)
    assert p.loc["2025-01-02", "all"] == pytest.approx(1.5)
    assert np.isnan(p.loc["2025-01-09", "near"]) and p.loc["2025-01-09", "all"] == pytest.approx(2.0)
    assert p.loc[pd.Timestamp("2025-01-10")].isna().all()


def test_transforms_use_the_sixty_sessions_before_t_and_the_one_session_change():
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2021-01-01", periods=80)
    x = pd.DataFrame({"near": rng.normal(1, 0.2, 80), "all": rng.normal(1, 0.1, 80)}, index=idx)
    tr = pc.transforms(x)
    assert list(tr.columns) == ["near_z60", "near_d1", "all_z60", "all_d1"]
    for i in (60, 79):
        w = x["near"].iloc[i - 60:i]
        assert tr["near_z60"].iloc[i] == pytest.approx((x["near"].iloc[i] - w.mean()) / w.std(ddof=1))
    assert tr["near_z60"].iloc[:60].isna().all() and tr["all_z60"].iloc[60:].notna().all()
    assert tr["all_d1"].iloc[5] == pytest.approx(x["all"].iloc[5] - x["all"].iloc[4])
    assert np.isnan(tr["all_d1"].iloc[0])
    later = x.copy()
    later.iloc[61:] = 99.0
    assert pc.transforms(later)["near_z60"].iloc[60] == pytest.approx(tr["near_z60"].iloc[60])
    flat = pd.DataFrame({"near": 1.0, "all": 1.0}, index=idx)
    assert pc.transforms(flat)["near_z60"].isna().all()


def test_outcomes_shift_by_position_on_the_joint_index():
    days = pd.bdate_range("2025-01-01", periods=12)
    spot = pd.DataFrame({"date": days, "open": np.arange(100.0, 112.0), "close": np.arange(200.0, 212.0)})
    spot = pd.concat([spot, pd.DataFrame({"date": [days[4]], "open": [104.0], "close": [204.0]})])
    spot.loc[spot["date"] == days[4], "open"] = [1.0, 104.0]
    opt_days = days.delete(2).append(pd.DatetimeIndex([pd.Timestamp("2025-02-01")]))
    o = pc.outcomes(spot, opt_days)
    assert list(o.index) == list(days.delete(2))
    assert list(o.columns) == ["r1", "r5", "past1", "past5"]
    assert o["r1"].iloc[0] == pytest.approx(np.log(103.0 / 101.0))
    assert o["r5"].iloc[0] == pytest.approx(np.log(107.0 / 101.0))
    assert o["r1"].iloc[2] == pytest.approx(np.log(105.0 / 104.0))
    assert o["past1"].iloc[2] == pytest.approx(np.log(203.0 / 201.0))
    assert o["past5"].iloc[6] == pytest.approx(np.log(207.0 / 201.0))
    assert np.isnan(o["past5"].iloc[4]) and np.isnan(o["r1"].iloc[-2])
    bad = spot.assign(open=np.where(spot["date"] == days[3], 0.0, spot["open"]))
    assert np.isnan(pc.outcomes(bad, opt_days)["r1"].iloc[0])


def zr(x):
    r = pd.Series(x).rank(method="average").to_numpy(float)
    r = r - r.mean()
    return r / np.sqrt((r @ r) / len(r))


def test_ic_is_pearson_on_ranks_and_t_is_nw_of_the_rank_product():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2022-01-03", periods=300)
    s = pd.Series(np.round(rng.normal(size=300), 1), index=idx)
    y = pd.Series(0.2 * s.to_numpy() + rng.normal(size=300), index=idx)
    s.iloc[3], y.iloc[10], y.iloc[11] = np.nan, np.inf, -np.inf
    ic, t = pc.ic_test(s, y, lags=5)
    ok = s.notna() & np.isfinite(y)
    zs, zo = zr(s[ok]), zr(y[ok])
    assert ic == pytest.approx(s[ok].rank().corr(y[ok].rank()))
    assert t == pytest.approx(newey_west_t(zs * zo, 5))
    assert t != pytest.approx(newey_west_t(zs * zo, 1))
    shuffled = y.sample(frac=1.0, random_state=2)
    assert pc.ic_test(s, shuffled, lags=5) == pytest.approx((ic, t))
    assert all(np.isnan(v) for v in pc.ic_test(pd.Series([1.0, 1.0, 1.0, 1.0]), pd.Series([1.0, 2, 3, 4]), 1))


def test_partial_ic_residualises_both_ranks_on_the_control_ranks():
    rng = np.random.default_rng(11)
    n = 1500
    past1, past5 = rng.normal(size=n), rng.normal(size=n)
    s = pd.Series(past1 + 0.1 * rng.normal(size=n))
    y = pd.Series(past1 + 0.5 * past5 + rng.normal(size=n))
    ctl = pd.DataFrame({"past1": past1, "past5": past5})
    ic, t = pc.ic_test(s, y, 1)
    icp, tp = pc.ic_test(s, y, 1, controls=ctl)
    assert ic > 0.4 and t > 10 and abs(tp) < 3
    r = np.column_stack([pd.Series(v).rank().to_numpy(float) for v in (s, y, past1, past5)])
    X = np.column_stack([np.ones(n), r[:, 2:]])
    e = r[:, :2] - X @ np.linalg.lstsq(X, r[:, :2], rcond=None)[0]
    es, eo = ((v - v.mean()) / v.std() for v in e.T)
    assert icp == pytest.approx(np.corrcoef(e[:, 0], e[:, 1])[0, 1])
    assert tp == pytest.approx(newey_west_t(es * eo, 1))


def test_run_tests_rows_lags_and_holm():
    rng = np.random.default_rng(5)
    n = 1500
    idx = pd.bdate_range("2019-04-01", periods=n)
    sig = pd.DataFrame({c: rng.normal(size=n) for c in ("near_z60", "near_d1", "all_z60", "all_d1")},
                       index=idx)
    out = pd.DataFrame({"r1": 0.4 * sig["near_z60"] + rng.normal(size=n), "r5": rng.normal(size=n),
                        "past1": rng.normal(size=n), "past5": rng.normal(size=n)}, index=idx)
    res = pc.run_tests(sig, out)
    assert list(res.index) == [f"{d}_{t}_r{h}" for d in ("near", "all") for t in ("z60", "d1")
                               for h in (1, 5)]
    assert {"ic", "t", "p", "ic_partial", "t_partial", "p_partial", "p_holm", "p_partial_holm",
            "survives", "n", "n_partial"} <= set(res.columns)
    for name, row in res.iterrows():
        c, h = name.rsplit("_r", 1)
        h = int(h)
        assert row["h"] == h
        assert (row["ic"], row["t"]) == pytest.approx(pc.ic_test(sig[c], out[f"r{h}"], lags=h))
        assert (row["ic_partial"], row["t_partial"]) == pytest.approx(
            pc.ic_test(sig[c], out[f"r{h}"], lags=h, controls=out[["past1", "past5"]]))
    assert np.allclose(res["p_holm"], holm(res["p"]))
    assert np.allclose(res["p_partial_holm"], holm(res["p_partial"]))
    assert res["survives"].tolist() == [True] + [False] * 7
    assert (res["n"] == n).all()


@pytest.mark.parametrize("raw, partial, survives", [
    (6.0, 6.0, True), (-6.0, -6.0, True), (6.0, -6.0, False), (6.0, 2.4, False),
    (2.4, 6.0, False), (2.6, 2.6, False)])
def test_survival_needs_both_bars_on_both_and_the_same_sign(monkeypatch, raw, partial, survives):
    def fake(signal, outcome, lags, controls=None):
        if signal.name == "near_z60" and outcome.name == "r1":
            return 0.1, (raw if controls is None else partial)
        return 0.0, 0.0
    monkeypatch.setattr(pc, "ic_test", fake)
    idx = pd.bdate_range("2020-01-01", periods=10)
    sig = pd.DataFrame(1.0, index=idx, columns=["near_z60", "near_d1", "all_z60", "all_d1"])
    out = pd.DataFrame(1.0, index=idx, columns=["r1", "r5", "past1", "past5"])
    res = pc.run_tests(sig, out)
    assert bool(res.loc["near_z60_r1", "survives"]) is survives
    assert not res["survives"].iloc[1:].any()


# --- assembly, audit and report -----------------------------------------------

def world(n=100, seed=0):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2019-01-01", periods=n)
    rows = []
    for d in days:
        for e in (d, d + pd.Timedelta(days=7), d + pd.Timedelta(days=35)):
            for r in ("PE", "CE"):
                rows.append(oi_row(d, e, r, int(rng.integers(1000, 5000))))
        rows.append(oi_row(d, d + pd.Timedelta(days=35), None, 10**6, kind="FUTIDX"))
    opts = pd.DataFrame(rows).astype({"right": object, "oi": "int64"})
    px = 24000 * np.exp(np.cumsum(rng.normal(0, 0.01, n + 2)))
    sdays = days.delete(30).append(pd.bdate_range(days[-1] + pd.Timedelta(days=1), periods=2))
    spot = pd.DataFrame({"date": sdays, "open": px[:len(sdays)], "high": px[:len(sdays)],
                         "low": px[:len(sdays)], "close": px[:len(sdays)] * 1.001})
    return opts, spot


def test_assemble_sample_runs_from_the_first_z60_to_six_sessions_before_the_end():
    opts, spot = world()
    p = pc.assemble(opts, spot)
    joint = pd.bdate_range("2019-01-01", periods=100).delete(30)
    assert list(p["sig"].index) == list(p["out"].index) == list(joint[60:len(joint) - 6])
    assert list(p["pcr"].index) == list(joint)
    n = p["counts"]
    assert (n["option_sessions"], n["spot_sessions"], n["joint_sessions"]) == (100, 101, 99)
    assert (n["option_sessions_without_spot"], n["option_rows_ignored"]) == (1, 100)
    assert n["spot_sessions_without_options"] == 0
    assert (n["sample_sessions"], n["sample_first"], n["sample_last"]) == (33, joint[60], joint[92])
    assert n["nan_signals"] == dict.fromkeys(["near_z60", "near_d1", "all_z60", "all_d1"], 0)
    assert n["nan_outcomes"] == dict.fromkeys(["r1", "r5", "past1", "past5"], 0)
    assert p["sig"].notna().all().all() and p["out"].notna().all().all()


def test_assemble_counts_nan_signals_and_honours_the_sample_start(monkeypatch):
    opts, spot = world()
    day = pd.bdate_range("2019-01-01", periods=100)[80]
    opts = opts[~((opts["date"] == day) & (opts["right"] == "CE"))]
    p = pc.assemble(opts, spot)
    nan = p["counts"]["nan_signals"]
    assert nan["near_d1"] == 2 and nan["all_d1"] == 2
    assert nan["near_z60"] == 14 and nan["all_z60"] == 14
    assert p["sig"].index[0] == pd.bdate_range("2019-01-01", periods=100).delete(30)[60]
    monkeypatch.setattr(pc, "SAMPLE_FROM", pd.Timestamp("2019-05-01"))
    q = pc.assemble(*world())
    assert q["sig"].index[0] == pd.Timestamp("2019-05-01")


def test_assemble_counts_spot_sessions_without_options_inside_the_joint_range():
    opts, spot = world()
    gap = pd.bdate_range("2019-01-01", periods=100)[50]
    p = pc.assemble(opts[opts["date"] != gap], spot)
    n = p["counts"]
    assert gap not in p["pcr"].index and n["joint_sessions"] == 98
    assert (n["spot_sessions_without_options"], n["option_sessions_without_spot"]) == (1, 1)
    assert "spot sessions inside it without options 1" in "\n".join(pc.audit_lines(p))


def test_audit_prints_counts_only():
    text = "\n".join(pc.audit_lines(pc.assemble(*world())))
    assert "sessions" in text and "sample" in text and "near_z60" in text and "r5" in text
    assert not re.search(r"\d\.\d", text)
    for word in ("ic", "mean", "corr", "t=", "nw", "p_"):
        assert not re.search(rf"\b{re.escape(word)}", text.lower())


def test_report_runs_end_to_end_and_is_json_clean():
    rep = pc.report(pc.assemble(*world(n=400)))
    assert set(rep) >= {"tests", "survivors", "counts", "constants", "pcr"}
    assert len(rep["tests"]) == 8 and isinstance(rep["survivors"], list)
    json.dumps(vc.clean(rep), allow_nan=False)
    lines = pc.report_lines(rep)
    assert len([ln for ln in lines if ln.startswith(("near_", "all_"))]) == 8


# --- main ------------------------------------------------------------------------

def never(*a, **k):
    raise AssertionError("loaded data")


def test_help_works_offline_and_loads_nothing(monkeypatch, capsys):
    monkeypatch.setattr(pc, "load_options", never)
    monkeypatch.setattr(pc, "load_history", never)
    with pytest.raises(SystemExit) as e:
        pc.main(["--help"])
    assert e.value.code == 0
    assert "--audit" in capsys.readouterr().out


def test_run_refuses_to_overwrite_a_previous_run(monkeypatch, tmp_path):
    out = tmp_path / "run.json"
    out.write_text("{}")
    monkeypatch.setattr(pc, "load_options", never)
    monkeypatch.setattr(pc, "load_history", never)
    with pytest.raises(SystemExit) as e:
        pc.main(["--run", "--out", str(out)])
    assert e.value.code != 0 and out.read_text() == "{}"


def test_run_refuses_to_refresh_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(pc, "load_options", never)
    monkeypatch.setattr(pc, "load_history", never)
    out = tmp_path / "run.json"
    with pytest.raises(SystemExit) as e:
        pc.main(["--run", "--refresh-cache", "--out", str(out)])
    assert e.value.code != 0 and not out.exists()


def test_run_refuses_tracked_changes_or_no_head(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pc, "load_options", never)
    monkeypatch.setattr(pc, "load_history", never)
    out = tmp_path / "run.json"
    for dirty, head in ((True, "abc1234"), (None, None), (False, None)):
        monkeypatch.setattr(pc, "_commit", lambda d=dirty, h=head: {"head": h, "dirty": d})
        with pytest.raises(SystemExit) as e:
            pc.main(["--run", "--out", str(out)])
        assert e.value.code != 0 and not out.exists()
        assert f"git HEAD {head}, tracked changes {dirty}" in capsys.readouterr().err


def test_run_refuses_an_output_under_the_owner_home(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vc, "OWNER_HOME", tmp_path / "owner")
    monkeypatch.setattr(pc, "_commit", lambda: {"head": "abc1234", "dirty": False})
    monkeypatch.setattr(pc, "load_options", never)
    monkeypatch.setattr(pc, "load_history", never)
    out = tmp_path / "owner" / "research" / "run.json"
    with pytest.raises(SystemExit) as e:
        pc.main(["--run", "--out", str(out)])
    assert e.value.code != 0 and not out.exists()
    assert "set SHUNKAN_HOME" in capsys.readouterr().err


def test_default_out_is_under_shunkan_home_not_the_owner_home():
    assert pc.DEFAULT_OUT.name == "pcr_daily_run.json"
    assert pc.DEFAULT_OUT.parent == vc.DEFAULT_OUT.parent

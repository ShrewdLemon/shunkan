import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
import vrp_condor as vc  # noqa: E402
from shunkan.backtest.options import DailyChain, Leg, black76_straddle  # noqa: E402
from shunkan.backtest.options import simulate, take_and_stop, take_profit  # noqa: E402


def opt_rows(day, expiry, strikes_close, contracts=5):
    out = []
    for (k, r), c in strikes_close.items():
        out.append(dict(date=pd.Timestamp(day), kind="OPTIDX", symbol="NIFTY",
                        expiry=pd.Timestamp(expiry), strike=float(k), right=r, open=c, high=c,
                        low=c, close=c, settle=c, contracts=contracts, oi=1000, oi_change=0,
                        underlying=np.nan, lot=np.nan))
    return out


def test_expiries_come_from_the_data_and_weeks_start_at_first_weekly():
    rows = []
    for d, e in [("2019-01-31", "2019-01-31"), ("2019-02-14", "2019-02-14"),
                 ("2019-02-21", "2019-02-21"), ("2019-02-20", "2019-02-21"),
                 ("2019-02-28", "2019-02-28")]:
        rows += opt_rows(d, e, {(11000, "CE"): 10.0})
    exps = vc.expiries(pd.DataFrame(rows))
    assert exps == [pd.Timestamp(x) for x in ("2019-01-31", "2019-02-14", "2019-02-21", "2019-02-28")]
    assert vc.weeks(exps) == [(pd.Timestamp("2019-02-14"), pd.Timestamp("2019-02-21")),
                              (pd.Timestamp("2019-02-21"), pd.Timestamp("2019-02-28"))]


def test_har_forecast_never_sees_after_entry():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2018-01-01", periods=400)
    v = pd.Series(rng.gamma(2.0, 5e-5, 400), index=idx)
    entry = idx[300]
    a = vc.har_forecast(v, entry, 5)
    v3 = v.copy()
    v3.iloc[301:] = 1.0
    assert vc.har_forecast(v3, entry, 5) == pytest.approx(a)
    assert a > 0


def test_build_week_strikes_wings_and_skip_reasons():
    F, T = 24000.0, 5 / 252
    sig = 0.12
    strad = black76_straddle(F, 24000.0, sig, T)
    m = sig * np.sqrt(T)
    ks = {(24000, "CE"): strad / 2, (24000, "PE"): strad / 2}
    for k in np.arange(22500, 25501, 50):
        ks.setdefault((k, "CE"), 5.0)
        ks.setdefault((k, "PE"), 5.0)
    ks[(round(F * np.exp(-m) / 50) * 50, "PE")] = 30.0
    ks[(round(F * np.exp(m) / 50) * 50, "CE")] = 30.0
    d0, exp = "2025-01-02", "2025-01-09"
    ch = DailyChain(pd.DataFrame(opt_rows(d0, exp, ks)), pd.Timestamp(exp))
    w = vc.build_week(ch, pd.Timestamp(d0), pd.Timestamp(exp), spot_close=24010.0, h=5,
                      sigma_hat=0.10)
    assert w.skip is None
    assert w.sigma == pytest.approx(sig, abs=1e-6)
    sp, sc = round(F * np.exp(-m) / 50) * 50, round(F * np.exp(m) / 50) * 50
    wp, wc = round(F * np.exp(-2 * m) / 50) * 50, round(F * np.exp(2 * m) / 50) * 50
    assert [(l.right, l.strike, l.qty) for l in w.legs] == [
        ("PE", wp, 1), ("PE", sp, -1), ("CE", sc, -1), ("CE", wc, 1)]
    assert w.vrp == pytest.approx(2.0, abs=1e-4)
    ch2 = DailyChain(pd.DataFrame(opt_rows(d0, exp, {(24000, "CE"): 50.0})), pd.Timestamp(exp))
    assert vc.build_week(ch2, pd.Timestamp(d0), pd.Timestamp(exp), 24010.0, 5, 0.1).skip == "no_atm"


# --- beyond the brief -------------------------------------------------------

D0, EXP = pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-09")
SIG, TT = 0.12, 5 / 252
MM = SIG * math.sqrt(TT)
SP, SC = 23600.0, 24400.0       # F e^(-/+ m) rounded to 50
WP, WC = 23200.0, 24850.0       # F e^(-/+ 2m) rounded to 50


def condor_strikes(short=30.0, wing=5.0, other=5.0, atm=None):
    strad = black76_straddle(24000.0, 24000.0, SIG, TT) if atm is None else atm
    ks = {(24000, "CE"): strad / 2, (24000, "PE"): strad / 2}
    for k in np.arange(22500, 25501, 50):
        ks.setdefault((k, "CE"), other)
        ks.setdefault((k, "PE"), other)
    ks[(SP, "PE")] = ks[(SC, "CE")] = short
    ks[(WP, "PE")] = ks[(WC, "CE")] = wing
    return ks


def chain_of(ks, day=D0, expiry=EXP, untraded=()):
    f = pd.DataFrame(opt_rows(day, expiry, ks))
    for k, r in untraded:
        f.loc[(f.strike == k) & (f.right == r), "contracts"] = 0
    return DailyChain(f, expiry)


def test_strike_constants_match_the_rounding():
    assert round(24000 * math.exp(-MM) / 50) * 50 == SP
    assert round(24000 * math.exp(MM) / 50) * 50 == SC
    assert round(24000 * math.exp(-2 * MM) / 50) * 50 == WP
    assert round(24000 * math.exp(2 * MM) / 50) * 50 == WC


def test_week_fields_and_gross_credit_on_entry_closes():
    w = vc.build_week(chain_of(condor_strikes()), D0, EXP, 24010.0, 5, 0.10)
    assert (w.entry, w.expiry, w.h) == (D0, EXP, 5)
    assert w.T == pytest.approx(5 / 252)
    assert (w.F, w.K_atm) == (pytest.approx(24000.0), 24000.0)
    assert w.sigma_hat == 0.10


def test_untraded_short_steps_outward():
    ks = condor_strikes()
    ks[(SP - 50, "PE")] = ks[(SC + 100, "CE")] = 30.0
    ch = chain_of(ks, untraded=[(SP, "PE"), (SC, "CE"), (SC + 50, "CE")])
    w = vc.build_week(ch, D0, EXP, 24010.0, 5, 0.10)
    assert w.skip is None
    assert [l.strike for l in w.legs] == [WP, SP - 50, SC + 100, WC]


def test_wing_must_lie_strictly_beyond_the_short():
    untraded = [(k, "PE") for k in np.arange(WP + 50, SP + 1, 50)]
    w = vc.build_week(chain_of(condor_strikes(), untraded=untraded), D0, EXP, 24010.0, 5, 0.10)
    assert w.skip is None
    assert [l.strike for l in w.legs[:2]] == [WP - 50, WP]


def test_skip_no_short_no_wing_no_credit_no_vol():
    strad = black76_straddle(24000.0, 24000.0, SIG, TT)
    atm_only = {(24000, "CE"): strad / 2, (24000, "PE"): strad / 2}
    assert vc.build_week(chain_of(atm_only), D0, EXP, 24010.0, 5, 0.1).skip == "no_short"
    shorts = dict(atm_only)
    shorts[(SP, "PE")] = shorts[(SC, "CE")] = 30.0
    assert vc.build_week(chain_of(shorts), D0, EXP, 24010.0, 5, 0.1).skip == "no_wing"
    w = vc.build_week(chain_of(condor_strikes(short=5.0, wing=10.0)), D0, EXP, 24010.0, 5, 0.1)
    assert w.skip == "no_credit" and w.legs is None
    w = vc.build_week(chain_of(condor_strikes(atm=14000.0)), D0, EXP, 24010.0, 5, 0.1)
    assert w.skip == "no_vol" and w.K_atm == 24000.0 and math.isnan(w.sigma)


def test_sessions_after_is_half_open():
    cal = list(pd.bdate_range("2025-01-01", "2025-01-10"))
    s = vc.sessions_after(cal, pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-09"))
    assert s == list(pd.bdate_range("2025-01-03", "2025-01-09"))
    assert len(s) == 5


def test_daily_variance_formula_and_bad_rows_dropped():
    spot = pd.DataFrame({"date": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03",
                                                 "2025-01-06"]),
                         "open": [100.0, 101.0, 102.0, 103.0],
                         "high": [101.0, 103.0, 0.0, 104.0],
                         "low": [99.0, 100.0, 101.0, 102.0],
                         "close": [100.5, 102.0, 102.5, 103.5]})
    v = vc.daily_variance(spot)
    k = 2 * math.log(2) - 1
    v1 = (0.5 * math.log(103 / 100) ** 2 - k * math.log(102 / 101) ** 2
          + math.log(101 / 100.5) ** 2)
    v3 = (0.5 * math.log(104 / 102) ** 2 - k * math.log(103.5 / 103) ** 2
          + math.log(103 / 102.5) ** 2)
    assert list(v.index) == list(pd.to_datetime(["2025-01-02", "2025-01-06"]))
    assert v.iloc[0] == pytest.approx(v1)
    assert v.iloc[1] == pytest.approx(v3)


def test_har_forecast_of_constant_variance_is_that_variance_and_needs_history():
    idx = pd.bdate_range("2018-01-01", periods=100)
    v = pd.Series(4e-5, index=idx)
    assert vc.har_forecast(v, idx[80], 5) == pytest.approx(4e-5)
    assert math.isnan(vc.har_forecast(v, idx[10], 5))


def test_har_forecast_matches_a_reference_ols():
    rng = np.random.default_rng(7)
    n, h, p = 260, 4, 230
    idx = pd.bdate_range("2018-01-01", periods=n)
    v = pd.Series(rng.gamma(2.0, 5e-5, n), index=idx)
    x = v.to_numpy()

    def reg(t):
        return [1.0, x[t], x[t - 4:t + 1].mean(), x[t - 21:t + 1].mean()]

    ts = range(21, p - h + 1)
    A = np.array([reg(t) for t in ts])
    b = np.array([x[t + 1:t + h + 1].mean() for t in ts])
    beta = np.linalg.solve(A.T @ A, A.T @ b)
    want = max(float(np.array(reg(p)) @ beta), 1e-10)
    assert vc.har_forecast(v, idx[p], h) == pytest.approx(want, rel=1e-8)


def test_har_forecast_uses_the_last_session_on_or_before_entry():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2018-01-01", periods=300)
    v = pd.Series(rng.gamma(2.0, 5e-5, 300), index=idx)
    fri = idx[204]
    assert fri.dayofweek == 4
    sat = fri + pd.Timedelta(days=1)
    assert vc.har_forecast(v, sat, 5) == pytest.approx(vc.har_forecast(v, fri, 5))


# --- Task 8: configurations, walk-forward and verdict -----------------------

def _toy_grid():
    idx = pd.to_datetime(["2019-06-06", "2020-06-04", "2021-06-03", "2022-03-03",
                          "2022-09-01", "2023-03-02", "2024-03-07", "2025-03-06", "2026-03-03"])
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame(0.0, index=idx, columns=cols)
    for s in vc.SLIPPAGES:
        g.loc[idx[:3], (4, s)] = [3.0, 2.0, 4.0]
        g.loc[idx[:3], (7, s)] = [1.0, 1.0, 1.2]
        g.loc[idx[3:], (4, s)] = [-5.0, -6.0, -5.0, -7.0, -6.0, -5.0]
        g.loc[idx[3:5], (9, s)] = [1e6, 1e6]
    return g


def test_walk_forward_selects_on_prior_years_only():
    g = _toy_grid()
    series, sel = vc.walk_forward(g, s=0.5)
    assert sel[2022] == 7
    assert sel[2023] == 7
    assert list(series.index) == list(g.index[3:])


def test_gating_and_skips_give_zero_rows():
    w = vc.Week(entry=pd.Timestamp("2025-01-02"), expiry=pd.Timestamp("2025-01-09"), h=5, T=5/252,
                F=24000, K_atm=24000, sigma=0.12, sigma_hat=0.115, legs=None, skip="no_wing")
    assert vc.week_pnl(w, None, [], 24000.0, (None, "E0"), 0.5) == 0.0
    w.skip, w.legs = None, [Leg("PE", 23200, 1)]
    assert vc.week_pnl(w, None, [], 24000.0, (2.0, "E0"), 0.5) == 0.0


def test_verdict_pass_rule_on_synthetic_series():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2022-01-06", periods=245, freq="W-THU")
    good = pd.Series(rng.normal(3.0, 4.0, 245), index=idx)
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame({c: good.values + 0.01 * c[0] for c in cols}, index=idx)
    v = vc.verdict(good, good - 1.0, g)
    assert v["pass1"] and v["pass3"] and v["pass4"]
    bad = vc.verdict(good * 0 - 1.0, good * 0 - 2.0, g)
    assert not bad["passes"]


# --- Task 8, beyond the brief ------------------------------------------------

D1, D2, D3, D4 = (pd.Timestamp(d) for d in ("2025-01-03", "2025-01-06", "2025-01-07",
                                            "2025-01-08"))
EXIT_DAYS = [D1, D2, D3, D4]


def test_constants_and_config_order():
    assert vc.CONFIGS[:4] == [(None, "E0"), (None, "E1"), (None, "E2"), (0.0, "E0")]
    assert len(vc.CONFIGS) == 12 and vc.CONFIGS[-1] == (4.0, "E2")
    assert (vc.LOT, vc.CHARGE_LOTS, vc.DSR_STD, vc.N_TRIALS) == (65, 1, "search", 15)
    assert vc.SLIPPAGES == (0.25, 0.5, 1.0)
    assert vc.exit_rule("E0") is None
    assert vc.exit_rule("E1")(50.0, 25.0) == "take" and vc.exit_rule("E1")(50.0, 26.0) is None
    assert vc.exit_rule("E2")(50.0, 125.0) == "stop" and vc.exit_rule("E2")(50.0, 124.0) is None
    with pytest.raises(ValueError):
        vc.exit_rule("E3")


def path_frame():
    """Entry D0 condor; D1 one leg missing; D2 loss of 1.6x credit; D3 cost 16 of 50."""
    rows = opt_rows(D0, EXP, condor_strikes())
    d1 = condor_strikes(short=70.0, wing=5.0)
    del d1[(SP, "PE")]
    rows += opt_rows(D1, EXP, d1)
    rows += opt_rows(D2, EXP, condor_strikes(short=70.0, wing=5.0))
    rows += opt_rows(D3, EXP, condor_strikes(short=10.0, wing=2.0))
    rows += opt_rows(D4, EXP, condor_strikes(short=10.0, wing=2.0))
    return pd.DataFrame(rows)


def path_week(sigma_hat=0.105):
    ch = DailyChain(path_frame(), EXP)
    return vc.build_week(ch, D0, EXP, 24010.0, 5, sigma_hat), ch


def test_week_pnl_runs_each_exit_through_simulate_at_lot_times_charge_lots(monkeypatch):
    w, ch = path_week()
    assert w.skip is None and w.vrp == pytest.approx(1.5, abs=1e-4)
    e0 = simulate(ch, w.legs, D0, EXP, EXIT_DAYS, 24010.0, 0.5, None, lot=65)
    e1 = simulate(ch, w.legs, D0, EXP, EXIT_DAYS, 24010.0, 0.5, take_profit(0.5), lot=65)
    e2 = simulate(ch, w.legs, D0, EXP, EXIT_DAYS, 24010.0, 0.5, take_and_stop(0.5, 1.5), lot=65)
    assert (e0.exit_kind, e1.exit_kind, e2.exit_kind) == ("settle", "take", "stop")
    assert (e1.exit_day, e2.exit_day) == (D3, D2)
    assert e0.gross == pytest.approx(50.0)
    for ex, r in zip(vc.EXITS, (e0, e1, e2)):
        assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (None, ex), 0.5) == pytest.approx(r.net)
        assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (0.0, ex), 0.5) == pytest.approx(r.net)
        assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (2.0, ex), 0.5) == 0.0
    monkeypatch.setattr(vc, "CHARGE_LOTS", 10)
    e0_10 = simulate(ch, w.legs, D0, EXP, EXIT_DAYS, 24010.0, 0.5, None, lot=650)
    assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (None, "E0"), 0.5) == pytest.approx(e0_10.net)
    assert e0_10.net > e0.net


def test_gate_is_strict_and_nan_vrp_never_passes_a_threshold():
    w, ch = path_week(sigma_hat=0.12)
    w.sigma = 0.12
    assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (0.0, "E0"), 0.5) == 0.0
    w.sigma_hat = math.nan
    assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (0.0, "E0"), 0.5) == 0.0
    assert vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, (None, "E0"), 0.5) != 0.0


def test_grid_layout_values_and_errors_propagate():
    w, ch = path_week()
    skipped = vc.Week(D0, pd.Timestamp("2025-01-16"), 5, 5 / 252, math.nan, math.nan, math.nan,
                      0.1, None, "no_atm")
    g = vc.grid([(w, ch, EXIT_DAYS, 24010.0), (skipped, None, [], math.nan)])
    assert g.shape == (2, 36)
    assert list(g.index) == [EXP, pd.Timestamp("2025-01-16")]
    assert list(g.columns[:4]) == [(0, 0.25), (0, 0.5), (0, 1.0), (1, 0.25)]
    for c, cfg in enumerate(vc.CONFIGS):
        for s in vc.SLIPPAGES:
            assert g.loc[EXP, (c, s)] == pytest.approx(
                vc.week_pnl(w, ch, EXIT_DAYS, 24010.0, cfg, s))
    assert (g.iloc[1] == 0.0).all()
    assert g.loc[EXP, (0, 0.25)] > g.loc[EXP, (0, 1.0)]
    with pytest.raises(ValueError):
        vc.grid([(w, ch, [D2, D1], 24010.0)])
    with pytest.raises(ValueError):
        vc.grid([(w, ch, EXIT_DAYS, math.nan)])


def test_reprice_follows_the_same_selections_and_oos_ends_at_registration():
    g = _toy_grid()
    late = pd.DataFrame(9.0, index=pd.to_datetime(["2026-10-06"]), columns=g.columns)
    g = pd.concat([g, late])
    series, sel = vc.walk_forward(g, s=0.5)
    assert series.index.max() == pd.Timestamp("2026-03-03")
    assert sorted(sel) == [2022, 2023, 2024, 2025, 2026]
    pd.testing.assert_series_equal(vc.reprice(g, sel, 0.5), series)
    g.loc[g.index[3:], (7, 1.0)] = -1.0
    r = vc.reprice(g, {y: 7 for y in sel}, 1.0)
    assert (r == -1.0).all() and len(r) == 6


def test_walk_forward_cannot_see_the_trading_year_or_later():
    g = _toy_grid()
    for s in vc.SLIPPAGES:
        g.loc[g.index[:3], (9, s)] = [1.0, 1.0, 1.0]
        g.loc[g.index[3:5], (9, s)] = [5.0, 5.0]
    sel = vc.walk_forward(g, 0.5)[1]
    assert sel[2022] == 7
    assert sel[2023] == 9


def test_walk_forward_ties_go_to_the_lowest_index():
    g = _toy_grid()
    for s in vc.SLIPPAGES:
        g[(2, s)] = g[(7, s)]
    assert vc.walk_forward(g, 0.5)[1][2022] == 2


def test_verdict_keys_dsr_std_and_split(monkeypatch):
    rng = np.random.default_rng(2)
    idx = pd.date_range("2022-01-06", periods=245, freq="W-THU")
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame(rng.normal(1.0, 4.0, (245, 36)), index=idx, columns=cols)
    s05 = g[(5, 0.5)].rename("net")
    v = vc.verdict(s05, g[(5, 1.0)], g)
    sh = [vc.sharpe(g[(c, 0.5)].to_numpy(), 52) for c in range(12)]
    assert v["dsr_std_used"] == pytest.approx(np.std(sh, ddof=1))
    assert v["n_weeks"] == 245
    assert v["mean_pre"] == pytest.approx(s05[s05.index < vc.SPLIT].mean())
    assert v["mean_post"] == pytest.approx(s05[s05.index >= vc.SPLIT].mean())
    assert v["nw_t_05"] == pytest.approx(vc.newey_west_t(s05.to_numpy(), 4))
    assert set(v) >= {"mean_05", "nw_t_05", "pass1", "dsr", "pass2", "mean_10", "pass3",
                      "mean_pre", "mean_post", "pass4", "passes", "n_weeks", "selections",
                      "dsr_std_used"}
    monkeypatch.setattr(vc, "DSR_STD", 1.0)
    v1 = vc.verdict(s05, g[(5, 1.0)], g)
    assert v1["dsr_std_used"] == 1.0
    assert v1["dsr_std_searched"] == pytest.approx(np.std(sh, ddof=1))
    assert v1["dsr"] == pytest.approx(
        vc.deflated_sharpe(s05, n_trials=15, trial_sharpe_std=1.0, periods=52).deflated)
    short = vc.verdict(s05.iloc[:10], s05.iloc[:10], g)
    assert math.isnan(short["dsr"]) and not short["pass2"] and not short["passes"]


def test_dsr_std_reads_out_of_sample_sharpes_only():
    rng = np.random.default_rng(6)
    idx = pd.date_range("2019-03-07", "2026-09-24", freq="W-THU")
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame(rng.normal(1.0, 4.0, (len(idx), 36)), index=idx, columns=cols)
    before = g.index < vc.OOS[0]
    g.loc[before, (3, 0.5)] = -50.0 + rng.normal(0.0, 1.0, before.sum())
    s05, sel = vc.walk_forward(g)
    v = vc.verdict(s05, vc.reprice(g, sel, 1.0), g)
    oos = g[~before]
    want = np.std([vc.sharpe(oos[(c, 0.5)].to_numpy(), 52) for c in range(12)], ddof=1)
    full = np.std([vc.sharpe(g[(c, 0.5)].to_numpy(), 52) for c in range(12)], ddof=1)
    assert v["dsr_std_searched"] == pytest.approx(want)
    assert v["dsr_std_used"] == pytest.approx(want)
    assert abs(full - want) > 0.5


def test_pass4_needs_both_halves_positive():
    idx = pd.date_range("2022-01-06", periods=245, freq="W-THU")
    rng = np.random.default_rng(4)
    x = pd.Series(rng.normal(5.0, 1.0, 245), index=idx)
    x[x.index >= vc.SPLIT] = -0.1
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame({c: x.values for c in cols}, index=idx)
    v = vc.verdict(x, x, g)
    assert v["mean_pre"] > 0 > v["mean_post"] and not v["pass4"] and not v["passes"]


# --- assembly from frames (what main feeds the grid) -------------------------

THU = [pd.Timestamp(d) for d in ("2025-01-02", "2025-01-09", "2025-01-16", "2025-01-23")]


def world():
    rows = opt_rows(THU[0], THU[0], {(24000, "CE"): 10.0})
    for a, b in zip(THU, THU[1:]):
        for d in pd.bdate_range(a, b):
            ks = condor_strikes()
            if b == THU[1] and d == D2:
                del ks[(SP, "PE")]
            rows += opt_rows(d, b, ks)
    opts = pd.DataFrame(rows)
    days = pd.bdate_range("2024-06-03", "2025-01-31")
    spot = pd.DataFrame({"date": days, "open": 24010.0, "high": 24100.0, "low": 23900.0,
                         "close": 24010.0})
    spot = spot[spot.date != THU[2]].reset_index(drop=True)
    vix = pd.DataFrame({"date": days, "close": 13.0})
    vix = vix[vix.date != THU[1]].reset_index(drop=True)
    return opts, spot, vix


def test_assemble_weeks_exit_days_settle_and_skips():
    p = vc.assemble(*world())
    rows, n = p["rows"], p["counts"]
    assert [(w.entry, w.expiry) for w, *_ in rows] == list(zip(THU, THU[1:]))
    w, ch, ex, st = rows[0]
    assert w.skip is None and w.h == 5 and ex == EXIT_DAYS and st == 24010.0
    assert [l.strike for l in w.legs] == [WP, SP, SC, WC]
    sig_hat = math.sqrt(252 * 0.5 * math.log(24100 / 23900) ** 2)
    assert w.sigma_hat == pytest.approx(sig_hat, rel=1e-6)
    assert rows[1][0].skip == "no_settle" and math.isnan(rows[1][3])
    assert rows[2][0].skip == "no_spot"
    assert (n["expiries"], n["weeks"], n["oos_weeks"]) == (4, 3, 3)
    assert n["skips"] == {"ok": 1, "no_settle": 1, "no_spot": 1}
    assert (n["no_settle_close"], n["no_entry_spot"], n["no_entry_vix"]) == (1, 1, 1)
    assert (n["h_lt3"], n["h_gt6"], n["har_nan"]) == (0, 0, 0)
    assert (n["exit_marks_none"], n["exit_marks"]) == (1, 4)
    assert (n["spot_first"], n["spot_last"], n["har_first"]) == (
        pd.Timestamp("2024-06-03"), pd.Timestamp("2025-01-31"), pd.Timestamp("2024-06-04"))
    assert (n["vix_first"], n["vix_last"]) == (pd.Timestamp("2024-06-03"), pd.Timestamp("2025-01-31"))
    assert p["vix"].iloc[0] == 13.0 and math.isnan(p["vix"].iloc[1])
    g = vc.grid(rows)
    assert g.loc[THU[1], (0, 0.5)] == pytest.approx(
        simulate(ch, w.legs, THU[0], THU[1], ex, st, 0.5, None, lot=65).net)
    assert (g.loc[THU[2:]] == 0.0).all().all()


def test_spot_only_session_counts_in_h_and_never_takes_an_exit():
    opts, spot, vix = world()
    opts = opts[opts.date != D2].reset_index(drop=True)
    assert len(vc.sessions_after(sorted(opts.date.unique()), THU[0], THU[1])) == 4
    p = vc.assemble(opts, spot, vix)
    w, ch, ex, st = p["rows"][0]
    assert w.skip is None and w.h == 5 and w.T == pytest.approx(5 / 252)
    assert ex == EXIT_DAYS and vc.mark(ch, D2, w.legs) is None
    n = p["counts"]
    assert (n["spot_sessions_added"], n["sessions"]) == (1, 16)
    assert (n["first_session"], n["last_session"]) == (THU[0], THU[3])
    assert (n["exit_marks_none"], n["exit_marks"]) == (1, 4)
    assert "sessions added from spot calendar 1" in vc.audit_lines(p)
    assert vc.assemble(*world())["counts"]["spot_sessions_added"] == 0
    r = simulate(ch, w.legs, THU[0], THU[1], ex, st, 0.5, take_profit(0.5), lot=65)
    assert (r.exit_kind, r.exit_day) == ("settle", THU[1])
    assert vc.week_pnl(w, ch, ex, st, (None, "E1"), 0.5) == pytest.approx(
        vc.week_pnl(w, ch, ex, st, (None, "E0"), 0.5))
    cheap = pd.concat([opts, pd.DataFrame(opt_rows(D2, THU[1], condor_strikes(10.0, 2.0)))])
    w2, ch2, ex2, st2 = vc.assemble(cheap, spot, vix)["rows"][0]
    assert (w2.h, ex2, [l.strike for l in w2.legs]) == (5, EXIT_DAYS, [l.strike for l in w.legs])
    r2 = simulate(ch2, w2.legs, THU[0], THU[1], ex2, st2, 0.5, take_profit(0.5), lot=65)
    assert (r2.exit_kind, r2.exit_day) == ("take", D2)


def test_assemble_forecasts_at_the_entry_date_not_the_expiry():
    opts, spot, vix = world()
    rng = np.random.default_rng(5)
    spot = spot.assign(high=24010.0 * np.exp(rng.uniform(0.002, 0.02, len(spot))),
                       low=24010.0 * np.exp(-rng.uniform(0.002, 0.02, len(spot))))
    w = vc.assemble(opts, spot, vix)["rows"][0][0]
    v = vc.daily_variance(spot)
    at_entry = math.sqrt(252 * vc.har_forecast(v, THU[0], 5))
    at_expiry = math.sqrt(252 * vc.har_forecast(v, THU[1], 5))
    assert w.sigma_hat == pytest.approx(at_entry, rel=1e-12)
    assert at_entry != pytest.approx(at_expiry, rel=1e-6)


def test_assemble_records_simulate_errors_as_skips(monkeypatch):
    def boom(*a, **k):
        raise ValueError("a leg did not trade at entry 2025-01-02")
    monkeypatch.setattr(vc, "simulate", boom)
    p = vc.assemble(*world())
    assert p["rows"][0][0].skip == "simulate: a leg did not trade at entry 2025-01-02"
    assert p["counts"]["skips"]["simulate"] == 1
    assert p["counts"]["skip_messages"] == {"simulate: a leg did not trade at entry 2025-01-02": 1}
    assert (vc.grid(p["rows"]) == 0.0).all().all()


def test_audit_prints_counts_only():
    text = "\n".join(vc.audit_lines(vc.assemble(*world())))
    assert "weeks" in text and "no_settle" in text
    assert ("spot dates 2024-06-03 to 2025-01-31, first HAR row 2024-06-04, "
            "VIX dates 2024-06-03 to 2025-01-31") in text
    assert not re.search(r"\d\.\d", text)
    for word in ("sigma", "vrp", "p&l", "pnl", "mean"):
        assert word not in text.lower()


def test_straddle_is_short_atm_held_to_settlement_with_the_same_charges():
    w, ch = path_week()
    legs = [Leg("CE", 24000.0, -1), Leg("PE", 24000.0, -1)]
    r = simulate(ch, legs, D0, EXP, EXIT_DAYS, 24010.0, 0.5, None, lot=65)
    assert vc.straddle_pnl(w, ch, EXIT_DAYS, 24010.0, 0.5) == pytest.approx(r.net)
    assert r.gross == pytest.approx(black76_straddle(24000.0, 24000.0, SIG, TT) - 10.0)
    assert vc.straddle_pnl(w, ch, EXIT_DAYS, math.nan, 0.5) == 0.0
    w.skip = "no_atm"
    assert vc.straddle_pnl(w, ch, EXIT_DAYS, 24010.0, 0.5) == 0.0


def test_stats_and_drawdown():
    assert vc.max_drawdown([1.0, -3.0, 1.0, -2.0, 5.0]) == pytest.approx(4.0)
    assert vc.max_drawdown([-1.0, 2.0]) == pytest.approx(1.0)
    st = vc.stats([1.0, -3.0, 1.0, -2.0, 5.0])
    assert (st["n"], st["worst"], st["max_dd"]) == (5, -3.0, 4.0)
    assert st["mean"] == pytest.approx(0.4) and st["cvar5"] == -3.0
    assert (st["mean_inr"], st["cvar5_inr"], st["worst_inr"]) == (
        pytest.approx(26.0), -195.0, -195.0)
    assert vc.stats([]) == {"n": 0}


def test_breakdowns_split_by_entry_vix_tercile_and_era():
    idx = pd.to_datetime(["2025-06-03", "2025-07-01", "2025-08-05", "2025-09-02", "2025-09-09",
                          "2025-09-16", "2025-09-23"])
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame(np.arange(7.0)[:, None] * np.ones((1, 36)), index=idx, columns=cols)
    vix = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, np.nan], index=idx)
    b = vc.breakdowns(g, vix)
    one = b["configs"]["none/E0"]
    assert {k: one["vix"][k]["n"] for k in ("low", "mid", "high", "none")} == {
        "low": 2, "mid": 2, "high": 2, "none": 1}
    assert one["vix"]["low"]["mean"] == pytest.approx(0.5)
    assert one["era"]["thursday"]["n"] == 3 and one["era"]["tuesday"]["n"] == 4
    assert b["vix_cuts"] == pytest.approx(list(np.quantile([10, 11, 12, 13, 14, 15], [1/3, 2/3])))


def test_report_runs_end_to_end_on_the_synthetic_world():
    rep = vc.report(vc.assemble(*world()))
    assert set(rep) >= {"verdict", "selected", "configs", "breakdowns", "straddle", "constants",
                        "counts", "weeks", "series"}
    assert rep["straddle"]["n_priced"] == 1
    assert rep["straddle"]["sigma_over_vix"]["n"] == 1
    assert rep["straddle"]["sigma_over_vix"]["mean"] == pytest.approx(SIG / 0.13, rel=1e-5)
    json.dumps(vc.clean(rep), allow_nan=False)
    assert vc.report_lines(rep)


# --- main and data paths ------------------------------------------------------

def test_help_works_offline_and_loads_nothing(monkeypatch, capsys):
    def never(*a, **k):
        raise AssertionError("loaded data")
    monkeypatch.setattr(vc, "load_options", never)
    monkeypatch.setattr(vc, "load_history", never)
    with pytest.raises(SystemExit) as e:
        vc.main(["--help"])
    assert e.value.code == 0
    assert "--audit" in capsys.readouterr().out


def test_load_options_caches_outside_the_owner_home(monkeypatch, tmp_path):
    calls = []
    frame = pd.DataFrame(opt_rows(D0, EXP, {(24000, "CE"): 10.0}))

    class FakeBhav:
        @staticmethod
        def load_range(start, end, symbols=None, kinds=None, root=None):
            calls.append((start, end, symbols, kinds, root))
            return frame

    monkeypatch.setattr(vc, "fo_bhav", FakeBhav)
    monkeypatch.setattr(vc, "OWNER_HOME", tmp_path / "owner")
    monkeypatch.setattr(vc, "OWNER_STORE", tmp_path / "owner" / "store")
    monkeypatch.setattr(vc, "CACHE", tmp_path / "owner" / "research" / "n.parquet")
    with pytest.raises(RuntimeError):
        vc.load_options()
    assert calls == []
    monkeypatch.setattr(vc, "CACHE", tmp_path / "scratch" / "research" / "n.parquet")
    a = vc.load_options()
    assert calls == [("2019-01-01", "2026-09-30", ["NIFTY"], ["OPTIDX"], tmp_path / "owner" / "store")]
    b = vc.load_options()
    assert len(calls) == 1 and vc.CACHE.exists()
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b, check_dtype=False)
    vc.load_options(refresh=True)
    assert len(calls) == 2
    monkeypatch.setattr(vc, "CACHE", tmp_path / "empty" / "n.parquet")
    frame = frame.iloc[:0]
    with pytest.raises(RuntimeError):
        vc.load_options()
    assert not (tmp_path / "empty").exists()


def test_load_history_normalises_dates(monkeypatch, tmp_path):
    (tmp_path / "history").mkdir()
    df = pd.DataFrame({"date": pd.to_datetime(["2025-01-03 15:30", "2025-01-02 00:00"]).astype(
        "datetime64[ms]"), "open": 1.0, "high": 1.0, "low": 1.0, "close": [2.0, 1.0]})
    df.to_parquet(tmp_path / "history" / "NIFTY.parquet")
    monkeypatch.setattr(vc, "OWNER_STORE", tmp_path)
    out = vc.load_history("NIFTY")
    assert str(out["date"].dtype) == "datetime64[ns]"
    assert list(out["date"]) == [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03")]
    assert list(out["close"]) == [1.0, 2.0]


def test_run_refuses_to_overwrite_a_previous_run(monkeypatch, tmp_path):
    out = tmp_path / "run.json"
    out.write_text("{}")
    monkeypatch.setattr(vc, "load_options", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    with pytest.raises(SystemExit) as e:
        vc.main(["--run", "--out", str(out)])
    assert e.value.code != 0 and out.read_text() == "{}"


def long_world():
    opts, _, vix = world()
    days = pd.bdate_range("2007-09-17", "2025-01-31")
    spot = pd.DataFrame({"date": days, "open": 24010.0, "high": 24100.0, "low": 23900.0,
                         "close": 24010.0})
    return opts, spot[spot.date != THU[2]].reset_index(drop=True), vix


def patch_main(monkeypatch, w, dirty=False, head="abc1234"):
    opts, spot, vix = w
    monkeypatch.setattr(vc, "load_options", lambda refresh=False: opts)
    monkeypatch.setattr(vc, "load_history", lambda name: spot if name == "NIFTY" else vix)
    monkeypatch.setattr(vc, "_commit", lambda: {"head": head, "dirty": dirty})


def test_run_refuses_tracked_changes_or_no_head(monkeypatch, tmp_path):
    for dirty, head in ((True, "abc1234"), (None, None), (False, None)):
        patch_main(monkeypatch, long_world(), dirty=dirty, head=head)
        monkeypatch.setattr(vc, "load_options", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("loaded data")))
        with pytest.raises(SystemExit) as e:
            vc.main(["--run", "--out", str(tmp_path / "run.json")])
        assert e.value.code != 0 and not (tmp_path / "run.json").exists()


def test_audit_runs_dirty_and_prints_the_flag(monkeypatch, capsys):
    patch_main(monkeypatch, long_world(), dirty=True)
    assert vc.main(["--audit"]) == 0
    text = capsys.readouterr().out
    assert "commit abc1234, tracked changes True" in text
    assert "first HAR row 2007-09-18" in text
    assert not re.search(r"\d\.\d", text)


def test_audit_and_run_refuse_a_har_fit_starting_after_2007_09(monkeypatch, tmp_path, capsys):
    patch_main(monkeypatch, world())
    for argv in (["--audit"], ["--run", "--out", str(tmp_path / "run.json")]):
        with pytest.raises(SystemExit) as e:
            vc.main(argv)
        assert e.value.code != 0
        assert "first HAR row 2024-06-04 is after 2007-09-30" in capsys.readouterr().err
    assert not (tmp_path / "run.json").exists()


def test_run_end_to_end_on_the_synthetic_world(monkeypatch, tmp_path, capsys):
    patch_main(monkeypatch, long_world())
    out = tmp_path / "run.json"
    assert vc.main(["--run", "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["commit"] == {"head": "abc1234", "dirty": False}
    assert rep["counts"]["har_first"] == "2007-09-18"
    st = rep["selected"]["s0.5"]
    assert st["mean_inr"] == pytest.approx(st["mean"] * 65)
    assert st["worst_inr"] == pytest.approx(st["worst"] * 65)
    assert "VERDICT" in capsys.readouterr().out


# --- final wave: guards and reporting ----------------------------------------

def test_run_refuses_to_refresh_the_cache(monkeypatch, tmp_path, capsys):
    def never(*a, **k):
        raise AssertionError("loaded data")
    monkeypatch.setattr(vc, "load_options", never)
    monkeypatch.setattr(vc, "load_history", never)
    monkeypatch.setattr(vc, "_commit", lambda: {"head": "abc1234", "dirty": False})
    out = tmp_path / "run.json"
    with pytest.raises(SystemExit) as e:
        vc.main(["--run", "--refresh-cache", "--out", str(out)])
    assert e.value.code != 0 and not out.exists()
    assert "refresh it with --audit first" in capsys.readouterr().err
    seen = []
    opts, spot, vix = long_world()
    patch_main(monkeypatch, (opts, spot, vix))
    monkeypatch.setattr(vc, "load_options", lambda refresh=False: seen.append(refresh) or opts)
    assert vc.main(["--audit", "--refresh-cache"]) == 0 and seen == [True]


def test_verdict_records_why_the_dsr_is_undefined():
    idx = pd.date_range("2022-01-06", periods=245, freq="W-THU")
    x = pd.Series(np.where(np.arange(245) % 2, 1.01, 1.0), index=idx)
    cols = pd.MultiIndex.from_product([range(12), vc.SLIPPAGES])
    g = pd.DataFrame({c: x.values for c in cols}, index=idx)
    assert math.isnan(vc.deflated_sharpe(x, n_trials=15, trial_sharpe_std=1.0,
                                         periods=52).deflated)
    v = vc.verdict(x, x, g)
    assert math.isnan(v["dsr"]) and not v["pass2"] and not v["passes"]
    assert v["pass1"] and v["pass3"] and v["pass4"]
    assert v["dsr_error"] == "denominator <= 0 (skew and kurtosis)"
    rep = vc.report(vc.assemble(*world()))
    short = rep["verdict"]["dsr_error"]
    assert short.startswith("need at least 20")
    assert f"DSR undefined: {short}" in "\n".join(vc.report_lines(rep))
    rep["verdict"] = v
    assert "DSR undefined: denominator <= 0" in "\n".join(vc.report_lines(rep))
    assert vc.clean(rep)["verdict"]["dsr_error"] == v["dsr_error"]
    y = pd.Series(np.random.default_rng(3).normal(1.0, 4.0, 245), index=idx)
    good = vc.verdict(y, y, g)
    assert good["dsr_error"] is None and math.isfinite(good["dsr"])
    rep["verdict"] = good
    assert "DSR undefined" not in "\n".join(vc.report_lines(rep))


def test_constants_block_records_the_har_start_guard():
    rep = vc.clean(vc.report(vc.assemble(*world())))
    assert rep["constants"]["HAR_START_BY"] == "2007-09-30"
    assert rep["constants"]["HAR_FROM"] == "2007-09-01"

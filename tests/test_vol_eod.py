import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from shunkan.analytics import vol_eod
from shunkan.backtest.options import DailyChain, strike_by_delta
from shunkan.data import fo_iv, rates
from shunkan.derivatives import black76

from test_fo_iv import R, chain, frame  # noqa: E402

D = date(2024, 8, 1)


@pytest.fixture(autouse=True)
def flat_rate(monkeypatch):
    monkeypatch.setattr(rates, "table", lambda: (np.array(["2018-08-01"], dtype="datetime64[D]"),
                                                 np.array([R])))


def panel(rows):
    return fo_iv.with_greeks(fo_iv.compute(frame(rows)))


def flat(v):
    return lambda F, K: v


def test_flat_smile_gives_one_number_everywhere():
    e = date(2024, 8, 29)
    out = vol_eod.by_expiry(panel(chain(D, e, 25040.0, range(22000, 28050, 50), vol=flat(0.15))))
    row = out.iloc[0]
    assert row["atm_iv"] == pytest.approx(0.15, abs=1e-9)
    assert row["put25_iv"] == pytest.approx(0.15, abs=1e-9)
    assert row["call25_iv"] == pytest.approx(0.15, abs=1e-9)
    assert row["skew25"] == pytest.approx(0.0, abs=1e-9)


def test_put_skew_is_positive_and_atm_is_the_smile_at_the_forward():
    e = date(2024, 8, 29)
    F = 25040.0
    out = vol_eod.by_expiry(panel(chain(D, e, F, range(22000, 28050, 50))))
    row = out.iloc[0]
    from test_fo_iv import smile
    assert row["atm_iv"] == pytest.approx(smile(F, F), abs=1e-4)
    assert row["skew25"] > 0.01


def test_no_25_delta_without_strikes_beyond_it():
    e = date(2024, 8, 29)
    out = vol_eod.by_expiry(panel(chain(D, e, 25040.0, range(24800, 25300, 50), vol=flat(0.15))))
    row = out.iloc[0]
    assert row["atm_iv"] == pytest.approx(0.15, abs=1e-9)
    assert np.isnan(row["put25_iv"]) and np.isnan(row["call25_iv"]) and np.isnan(row["skew25"])


def test_atm_needs_a_put_and_a_call_close_to_the_forward():
    e = date(2024, 8, 29)
    strikes = [k for k in range(22000, 28050, 50) if not 23500 <= k <= 26500]
    out = vol_eod.by_expiry(panel(chain(D, e, 25040.0, strikes, vol=flat(0.15))))
    assert np.isnan(out.iloc[0]["atm_iv"])


def test_constant_maturity_interpolates_total_variance():
    days = [pd.Timestamp(D)] * 3
    e = pd.DataFrame({"date": days, "symbol": "NIFTY",
                      "expiry": pd.to_datetime(["2024-08-02", "2024-08-21", "2024-09-10"]),
                      "t": [1 / 365, 20 / 365, 40 / 365], "forward": 25000.0,
                      "atm_iv": [0.40, 0.12, 0.16], "skew25": [0.0, 0.02, 0.04],
                      "share": [1.0, 0.5, 0.2]})
    out = vol_eod.constant_maturity(e, 30).iloc[0]
    var = 0.12**2 * 20 / 365 + 0.5 * (0.16**2 * 40 / 365 - 0.12**2 * 20 / 365)
    assert out["atm_iv"] == pytest.approx(math.sqrt(var / (30 / 365)))
    assert out["skew25"] == pytest.approx(0.03)
    assert out["expiry_lo"] == pd.Timestamp("2024-08-21")
    assert vol_eod.constant_maturity(e, 60).empty
    dead = e.assign(share=[1.0, 0.5, 0.0001])
    assert vol_eod.constant_maturity(dead, 30).empty


def test_a_one_lot_stale_print_barely_moves_atm():
    e = date(2024, 8, 29)
    rows = chain(D, e, 25040.0, range(22000, 28050, 50), vol=flat(0.15))
    for r in rows:
        r["contracts"] = 500
        if r["strike"] == 25050.0 and r["right"] == "CE":
            r["close"] *= 0.3
            r["contracts"] = 1
    assert vol_eod.by_expiry(panel(rows)).iloc[0]["atm_iv"] == pytest.approx(0.15, abs=0.002)


def test_constant_maturity_from_a_real_panel_lands_between_its_expiries():
    rows = (chain(D, date(2024, 8, 22), 25040.0, range(22000, 28050, 50), vol=flat(0.12))
            + chain(D, date(2024, 9, 12), 25080.0, range(22000, 28050, 50), vol=flat(0.16)))
    cm = vol_eod.constant_maturity(vol_eod.by_expiry(panel(rows)), 30).iloc[0]
    assert 0.12 < cm["atm_iv"] < 0.16


def test_rank_and_percentile_use_only_the_window_ending_today():
    s = pd.Series([0.10, 0.20, 0.15, 0.30, 0.12])
    r = vol_eod.iv_rank(s, 3)
    assert r.isna().tolist()[:2] == [True, True]
    assert r.iloc[2] == pytest.approx(0.5) and r.iloc[3] == pytest.approx(1.0)
    assert r.iloc[4] == pytest.approx(0.0)
    p = vol_eod.iv_percentile(s, 3)
    assert p.iloc[2] == pytest.approx(0.5) and p.iloc[4] == pytest.approx(0.0)
    later = s.copy()
    later.iloc[4] = 9.0
    assert vol_eod.iv_rank(later, 3).iloc[:4].equals(r.iloc[:4])


def test_variance_premium_lines_up_the_next_sessions():
    idx = pd.bdate_range("2024-01-01", periods=30)
    rng = np.random.default_rng(3)
    close = pd.Series(25000 * np.exp(np.cumsum(rng.normal(0, 0.01, 30))), index=idx)
    iv = pd.Series(0.15, index=idx)
    out = vol_eod.variance_premium(iv, close, horizon=5)
    r2 = np.log(close / close.shift(1)) ** 2
    assert out["realized_var_next"].iloc[3] == pytest.approx(r2.iloc[4:9].sum() * 252 / 5)
    assert out["rv_trailing"].iloc[10] == pytest.approx(math.sqrt(r2.iloc[6:11].sum() * 252 / 5))
    assert out["realized_var_next"].iloc[-5:].isna().all()
    assert out["premium_var"].iloc[3] == pytest.approx(0.0225 - out["realized_var_next"].iloc[3])


def test_strike_by_delta_picks_the_nearest_traded_delta():
    e = date(2024, 8, 29)
    p = panel(chain(D, e, 25040.0, range(22000, 28050, 50), untraded={(24400, "PE")}))
    ch = DailyChain(p, e)
    k = strike_by_delta(ch, D, "PE", -0.25)
    d = {s: ch.delta(D, s, "PE") for s in ch.strikes(D) if ch.traded(D, s, "PE")}
    assert k == min(d, key=lambda s: abs(d[s] + 0.25))
    assert abs(d[k] + 0.25) < 0.02
    assert strike_by_delta(ch, D, "CE", 0.25) > 25040.0
    assert strike_by_delta(ch, D, "PE", -0.25, tol=1e-9) is None
    with pytest.raises(ValueError):
        strike_by_delta(ch, D, "PE", 0.25)
    plain = DailyChain(p.drop(columns=fo_iv.GREEKS), e)
    with pytest.raises(ValueError, match="no deltas"):
        strike_by_delta(plain, D, "PE", -0.25)


def test_codes_other_than_ok_never_feed_the_surface():
    e = date(2024, 8, 29)
    p = panel(chain(D, e, 25040.0, range(22000, 28050, 50), vol=flat(0.15)))
    p.loc[p.index[:5], "code"] = black76.NOT_CONVERGED
    p.loc[p.index[:5], "iv"] = 9.0
    assert vol_eod.by_expiry(p).iloc[0]["atm_iv"] == pytest.approx(0.15, abs=1e-9)

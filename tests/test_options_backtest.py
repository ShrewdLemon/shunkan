import math

import numpy as np
import pandas as pd
import pytest

from shunkan.backtest import costs
from shunkan.backtest.options import (DailyChain, Leg, black76_straddle, intrinsic, mark,
                                      nearest_traded, parity_forward, simulate, straddle_vol,
                                      take_and_stop, take_profit)

D0 = pd.Timestamp("2025-01-02")
EXP = pd.Timestamp("2025-01-09")


def chain_frame(rows):
    """rows: (date, strike, right, close, contracts)."""
    out = []
    for d, k, r, c, n in rows:
        out.append(dict(date=pd.Timestamp(d), kind="OPTIDX", symbol="NIFTY", expiry=EXP,
                        strike=float(k), right=r, open=c, high=c, low=c, close=c, settle=c,
                        contracts=n, oi=1000, oi_change=0, underlying=np.nan, lot=np.nan))
    return pd.DataFrame(out)


def test_intrinsic():
    assert intrinsic(Leg("CE", 24400, -1), 24550) == 150
    assert intrinsic(Leg("PE", 23600, -1), 24550) == 0
    assert intrinsic(Leg("PE", 23600, 1), 23000) == 600


def test_nearest_traded_rounds_then_steps_outward():
    f = chain_frame([(D0, 24400, "CE", 10, 0), (D0, 24450, "CE", 8, 5), (D0, 24350, "CE", 12, 9)])
    ch = DailyChain(f, EXP)
    assert nearest_traded(ch, D0, 24412.0, "CE", direction=1) == 24450
    assert nearest_traded(ch, D0, 24412.0, "CE", direction=1, max_steps=0) is None
    assert nearest_traded(ch, D0, 24390.0, "CE", direction=1, beyond=24400) == 24450


def test_parity_forward_uses_strike_nearest_spot_with_both_sides_traded():
    f = chain_frame([(D0, 24000, "CE", 120, 10), (D0, 24000, "PE", 100, 10),
                     (D0, 24050, "CE", 95, 10), (D0, 24050, "PE", 0, 0)])
    F, K, C, P = parity_forward(DailyChain(f, EXP), D0, spot=24040.0)
    assert (K, C, P) == (24000, 120, 100)
    assert F == pytest.approx(24020.0)


def test_straddle_vol_inverts_black76():
    F, K, T = 24020.0, 24000.0, 5 / 252
    price = black76_straddle(F, K, 0.13, T)
    assert straddle_vol(F, K, price, T) == pytest.approx(0.13, abs=1e-6)
    assert math.isnan(straddle_vol(F, K, 10.0, T))


def test_mark_needs_every_leg_traded():
    f = chain_frame([(D0, 23600, "PE", 40, 3), (D0, 24400, "CE", 35, 0)])
    ch = DailyChain(f, EXP)
    legs = [Leg("PE", 23600, -1)]
    assert mark(ch, D0, legs) == -40
    assert mark(ch, D0, legs + [Leg("CE", 24400, -1)]) is None


def test_chain_keeps_only_its_expiry_and_options():
    f = chain_frame([(D0, 24000, "CE", 120, 10), (D0, 24000, "PE", 100, 10)])
    fut = f.iloc[[0]].assign(kind="FUTIDX", strike=np.nan, right=None, close=24050.0)
    other = f.assign(expiry=pd.Timestamp("2025-01-16"), close=999.0)
    ch = DailyChain(pd.concat([f, fut, other], ignore_index=True), EXP)
    assert ch.quote(D0, 24000, "CE") == (120.0, 10)
    assert list(ch.strikes(D0)) == [24000.0]
    assert ch.days() == [D0]
    assert ch.quote(D0, 24050, "CE") is None and not ch.traded(D0, 24050, "CE")
    with pytest.raises(ValueError):
        DailyChain(pd.concat([f, f], ignore_index=True), EXP)


def test_straddle_vol_has_no_solution_beyond_the_bracket():
    F, K, T = 24020.0, 24000.0, 5 / 252
    assert math.isnan(straddle_vol(F, K, 20.0, T))
    assert math.isnan(straddle_vol(F, K, black76_straddle(F, K, 5.0, T), T))
    assert black76_straddle(F, K, 0.0, T) == pytest.approx(20.0)


CONDOR = [Leg("PE", 23200, 1), Leg("PE", 23600, -1), Leg("CE", 24400, -1), Leg("CE", 24800, 1)]
ENTRY = {23200: ("PE", 10), 23600: ("PE", 40), 24400: ("CE", 35), 24800: ("CE", 8)}


def condor_chain(day_marks):
    """day_marks: {day: {strike: close}}; every leg traded on each listed day."""
    rows = [(D0, k, r, c, 5) for k, (r, c) in ENTRY.items()]
    for d, m in day_marks.items():
        rows += [(d, k, ENTRY[k][0], c, 5) for k, c in m.items()]
    return DailyChain(chain_frame(rows), EXP)


def test_condor_settles_at_intrinsic_with_entry_charges_only():
    ch = condor_chain({})
    r = simulate(ch, CONDOR, D0, EXP, [], settle=24000.0, slippage=0.5, lot=65)
    assert r.exit_kind == "settle"
    assert r.credit == pytest.approx(57.0)
    assert r.gross == pytest.approx(57.0)
    assert r.slippage == pytest.approx(2.0)
    fills = [costs.Fill("BUY", 10.5, 65), costs.Fill("SELL", 39.5, 65),
             costs.Fill("SELL", 34.5, 65), costs.Fill("BUY", 8.5, 65)]
    assert r.charges == pytest.approx(costs.cost_of(fills, on=D0).total / 65)
    assert r.charges == pytest.approx(101.7885 / 65, rel=1e-4)
    assert r.net == pytest.approx(57.0 - 2.0 - r.charges)


def test_max_loss_settlement_charges_exercise_stt_on_the_long_wing():
    ch = condor_chain({})
    r = simulate(ch, CONDOR, D0, EXP, [], settle=24900.0, slippage=0.0, lot=65)
    assert r.gross == pytest.approx(57.0 - 400.0)
    entry_only = simulate(ch, CONDOR, D0, EXP, [], settle=24000.0, slippage=0.0, lot=65).charges
    assert r.charges - entry_only == pytest.approx(costs.schedule_on(EXP).stt_exercise_intrinsic * 100 * 65 / 65)
    assert costs.schedule_on(EXP) is costs.UNTIL_MARCH_2026


def test_take_profit_closes_at_half_credit():
    d1, d2 = pd.Timestamp("2025-01-03"), pd.Timestamp("2025-01-06")
    ch = condor_chain({d1: {23200: 8, 23600: 30, 24400: 30, 24800: 7},
                       d2: {23200: 4, 23600: 15, 24400: 15, 24800: 4}})
    r = simulate(ch, CONDOR, D0, EXP, [d1, d2], settle=24000.0, slippage=0.5,
                 exit_rule=take_profit(0.5), lot=65)
    assert r.exit_kind == "take" and r.exit_day == d2
    assert r.gross == pytest.approx(57.0 - 22.0)
    assert r.slippage == pytest.approx(4.0)


def test_stop_fires_on_loss_of_one_and_a_half_credits():
    d1 = pd.Timestamp("2025-01-03")
    ch = condor_chain({d1: {23200: 1, 23600: 2, 24400: 230, 24800: 80}})
    r = simulate(ch, CONDOR, D0, EXP, [d1], settle=24000.0, slippage=0.0,
                 exit_rule=take_and_stop(0.5, 1.5), lot=65)
    assert r.exit_kind == "stop"
    assert r.gross == pytest.approx(57.0 - 151.0)


def test_untraded_leg_day_makes_no_decision():
    d1 = pd.Timestamp("2025-01-03")
    rows = [(D0, k, r, c, 5) for k, (r, c) in ENTRY.items()]
    rows += [(d1, 23200, "PE", 1, 5), (d1, 23600, "PE", 1, 5), (d1, 24400, "CE", 1, 0),
             (d1, 24800, "CE", 1, 5)]
    ch = DailyChain(chain_frame(rows), EXP)
    r = simulate(ch, CONDOR, D0, EXP, [d1], settle=24000.0, slippage=0.0,
                 exit_rule=take_profit(0.5), lot=65)
    assert r.exit_kind == "settle"


def test_entry_with_an_untraded_leg_is_refused():
    rows = [(D0, k, r, c, 0 if k == 24800 else 5) for k, (r, c) in ENTRY.items()]
    with pytest.raises(ValueError):
        simulate(DailyChain(chain_frame(rows), EXP), CONDOR, D0, EXP, [], 24000.0, 0.5)


def test_a_sell_fill_is_floored_at_zero():
    ch = DailyChain(chain_frame([(D0, 23000, "PE", 0.3, 5)]), EXP)
    r = simulate(ch, [Leg("PE", 23000, -1)], D0, EXP, [], 24000.0, 0.5)
    assert r.gross == pytest.approx(0.3) and r.slippage == pytest.approx(0.3)
    assert r.charges == pytest.approx(costs.cost_of([costs.Fill("SELL", 0.0, 65)], on=D0).total / 65)


def test_each_fill_pays_the_rates_of_its_own_day():
    e, x, exp = pd.Timestamp("2026-03-31"), pd.Timestamp("2026-04-01"), pd.Timestamp("2026-04-07")
    f = chain_frame([(e, 23600, "PE", 40, 5), (x, 23600, "PE", 30, 5)]).assign(expiry=exp)
    r = simulate(DailyChain(f, exp), [Leg("PE", 23600, -1)], e, exp, [x], 24000.0, 0.0,
                 exit_rule=take_profit(0.9))
    assert r.exit_kind == "take"
    opened = costs.cost_of([costs.Fill("SELL", 40.0, 65)], on=e)
    closed = costs.cost_of([costs.Fill("BUY", 30.0, 65)], on=x)
    assert opened.stt == pytest.approx(2.60)
    assert r.charges == pytest.approx((opened.total + closed.total) / 65)


def test_quantity_scales_fills_and_an_itm_short_pays_no_exercise_stt():
    ch = DailyChain(chain_frame([(D0, 23600, "PE", 40, 5)]), EXP)
    r = simulate(ch, [Leg("PE", 23600, -2)], D0, EXP, [], 23500.0, 0.0)
    assert r.credit == pytest.approx(80.0) and r.gross == pytest.approx(80.0 - 200.0)
    assert r.charges == pytest.approx(costs.cost_of([costs.Fill("SELL", 40.0, 130)], on=D0).total / 65)


def test_exit_days_settle_and_expiry_are_checked():
    ch = condor_chain({})
    d1, d2 = pd.Timestamp("2025-01-03"), pd.Timestamp("2025-01-06")
    for days in ([EXP], [D0], [d2, d1]):
        with pytest.raises(ValueError):
            simulate(ch, CONDOR, D0, EXP, days, 24000.0, 0.5, exit_rule=take_profit())
    with pytest.raises(ValueError):
        simulate(ch, CONDOR, D0, pd.Timestamp("2025-01-16"), [], 24000.0, 0.5)
    with pytest.raises(ValueError):
        simulate(ch, CONDOR, D0, EXP, [], float("nan"), 0.5)

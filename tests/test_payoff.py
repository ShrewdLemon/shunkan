import numpy as np
import pytest

from shunkan.derivatives import (
    Leg,
    analyze_payoff,
    build_strategy,
    parse_custom_legs,
    synthetic_chain,
)


@pytest.fixture(scope="module")
def chain():
    return synthetic_chain("NIFTY")


def test_long_straddle_shape(chain):
    a = build_strategy(chain, "long_straddle")
    # Debit strategy: pays premium, limited loss, unlimited-ish profit.
    assert a.net_premium < 0
    assert a.max_profit == float("inf")
    assert a.max_loss < 0 and a.max_loss != float("-inf")
    assert len(a.breakevens) == 2
    lo, hi = sorted(a.breakevens)
    assert lo < a.spot < hi


def test_short_straddle_mirror(chain):
    long_a = build_strategy(chain, "long_straddle")
    short_a = build_strategy(chain, "short_straddle")
    assert short_a.net_premium == pytest.approx(-long_a.net_premium)
    # Short straddle: limited profit (the credit), unbounded loss.
    assert short_a.max_loss == float("-inf")
    assert short_a.max_profit == pytest.approx(-long_a.max_loss)


def test_iron_condor_defined_risk(chain):
    a = build_strategy(chain, "iron_condor")
    assert len(a.legs) == 4
    assert a.net_premium > 0  # credit
    assert a.max_profit != float("inf")
    assert a.max_loss != float("-inf")
    assert a.max_profit > 0 > a.max_loss
    assert 0.0 < a.pop < 1.0


def test_bull_call_spread_caps_both_sides(chain):
    a = build_strategy(chain, "bull_call_spread")
    assert a.max_profit != float("inf")
    assert a.max_loss != float("-inf")
    # Loss capped at net debit. net_premium is always per unit; max_loss is
    # per lot when a source named the lot and per unit when none could.
    assert a.max_loss == pytest.approx(a.net_premium * (a.lot_size or 1), rel=1e-6)


def test_unknown_lot_yields_per_unit_money_not_a_guess(chain):
    """A guessed lot silently multiplies every rupee figure, so an unknown
    lot must fall back to per-unit money and say so — never to a default."""
    chain.lot_size = None
    a = build_strategy(chain, "bull_call_spread")
    assert a.lot_size is None  # renderers read this to label the unit
    assert a.max_loss == pytest.approx(a.net_premium, rel=1e-6)


def test_payoff_at_known_points():
    from shunkan.derivatives.chain import OptionChain
    # Hand-built single long call: strike 100, premium 5.
    leg = Leg(side=+1, kind="CE", strike=100.0, premium=5.0, iv=0.2)
    assert leg.payoff(np.array([90.0]))[0] == pytest.approx(-5.0)
    assert leg.payoff(np.array([105.0]))[0] == pytest.approx(0.0)
    assert leg.payoff(np.array([120.0]))[0] == pytest.approx(15.0)


def test_position_greeks_signs(chain):
    short = build_strategy(chain, "short_straddle")
    # Short ATM straddle: positive theta (collects decay), negative gamma/vega.
    assert short.greeks["theta"] > 0
    assert short.greeks["gamma"] < 0
    assert short.greeks["vega"] < 0


def test_parse_custom_legs(chain):
    atm = float(chain.strikes[chain.atm_index])
    step = float(chain.strikes[1] - chain.strikes[0])
    spec = [f"+{atm:g}CE", f"-{atm + step:g}CE"]
    legs = parse_custom_legs(chain, spec)
    assert legs[0].side == +1 and legs[0].kind == "CE"
    assert legs[1].side == -1
    a = analyze_payoff(chain, legs, name="custom")
    assert a.max_loss != float("-inf")  # spread is defined-risk


def test_parse_custom_legs_rejects_garbage(chain):
    with pytest.raises(ValueError):
        parse_custom_legs(chain, ["23200CE"])  # missing +/-
    with pytest.raises(ValueError):
        parse_custom_legs(chain, ["+99999999CE"])  # strike not in chain


def test_unknown_strategy_raises(chain):
    with pytest.raises(KeyError, match="Unknown strategy"):
        build_strategy(chain, "infinite_money_glitch")


def test_pop_sane_for_wide_condor(chain):
    narrow = build_strategy(chain, "iron_condor", width=1)
    wide = build_strategy(chain, "iron_condor", width=3)
    # Wider short strikes => higher probability the spot stays inside.
    assert wide.pop > narrow.pop

"""The Indian F&O cost stack.

These exist because understating cost is the same class of error as
fabricating a price: both make an unprofitable strategy look fine.
"""

from __future__ import annotations

import pytest

from shunkan.backtest.costs import (
    BROKERAGE_PER_ORDER,
    Fill,
    breakeven_edge,
    cost_of,
    round_trip,
)

LOT = 65


def straddle(lots=1, premium=59.75):
    q = LOT * lots
    return [Fill("SELL", premium, q), Fill("SELL", premium, q)]


def condor(lots=1):
    q = LOT * lots
    return [Fill("SELL", 40, q), Fill("BUY", 15, q),
            Fill("SELL", 40, q), Fill("BUY", 15, q)]


def test_brokerage_is_flat_per_order_not_a_percentage():
    """Zerodha's 'Rs 20 or 0.03%, whichever is lower' is the EQUITY INTRADAY
    rule. Applying it to F&O turns a Rs 20 charge into Rs 1.17 on a one-lot
    leg and quietly makes every small structure look viable."""
    small = cost_of([Fill("SELL", 1.0, LOT)])
    large = cost_of([Fill("SELL", 500.0, LOT)])
    assert small.brokerage == BROKERAGE_PER_ORDER
    assert large.brokerage == BROKERAGE_PER_ORDER


def test_flat_brokerage_punishes_small_multi_leg_structures():
    """The finding that matters: 'the strategy works, just size down' is
    backwards in Indian options. Eight fixed charges do not shrink."""
    one = 100 * round_trip(condor(1)).total / (50 * LOT * 1)
    fifty = 100 * round_trip(condor(50)).total / (50 * LOT * 50)
    assert one > 5.0            # over 5% of credit at one lot
    assert fifty < 1.0          # under 1% at fifty
    assert one > 8 * fifty      # and the gap is an order of magnitude


def test_a_condor_costs_more_than_a_straddle_at_the_same_size():
    """Four legs, eight orders, twice the fixed charge."""
    assert round_trip(condor(5)).brokerage == 2 * round_trip(straddle(5)).brokerage


def test_stt_falls_on_the_sell_side_only():
    sell = cost_of([Fill("SELL", 100.0, LOT)])
    buy = cost_of([Fill("BUY", 100.0, LOT)])
    assert sell.stt > 0 and buy.stt == 0
    assert buy.stamp > 0 and sell.stamp == 0     # stamp is the mirror image


def test_exercise_stt_is_charged_on_intrinsic_not_premium():
    """The line that ruins people. A long option left to expire ITM is taxed
    on settlement value, which can dwarf what the option cost."""
    premium_paid = 5.0 * LOT                      # Rs 325 for the option
    closed = cost_of([Fill("BUY", 5.0, LOT)])
    expired_itm = cost_of([Fill("BUY", 5.0, LOT, intrinsic=500.0)])

    assert closed.stt == 0                        # buying incurs no STT
    # Rs 40.63 of tax on an option that cost Rs 325: 12.5% of the premium,
    # levied because it was allowed to expire rather than sold.
    assert expired_itm.stt / premium_paid > 0.10
    assert expired_itm.total > closed.total + 0.10 * premium_paid


def test_breakeven_edge_is_quoted_as_a_fraction_of_credit():
    """Every backtest should be quoted net of this number."""
    assert breakeven_edge(condor(1)) > breakeven_edge(condor(50))
    assert 0 < breakeven_edge(straddle(10)) < 0.01


def test_gst_applies_to_brokerage_and_charges_not_to_stt():
    c = cost_of(straddle(1))
    assert c.gst == pytest.approx(0.18 * (c.brokerage + c.exchange + c.sebi))

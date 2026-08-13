"""Net book risk — the numbers a desk watches instead of a position list."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from shunkan.derivatives.synthetic import synthetic_chain
from shunkan.portfolio import Book, Instrument
from shunkan.portfolio.book import BUY, SELL
from shunkan.portfolio.risk import book_greeks, describe


@pytest.fixture
def chain():
    c = synthetic_chain("NIFTY")
    c.ensure_iv()
    return c


@pytest.fixture
def chains(chain):
    return {("NIFTY", str(chain.expiry)): chain}


def atm(chain, right):
    k = float(chain.strikes[chain.atm_index])
    return Instrument.option("NIFTY", chain.expiry, k, right, lot_size=65)


def test_short_straddle_is_delta_flat_and_short_gamma(chain, chains):
    """The whole reason a desk reads net Greeks: two short legs with real
    per-leg deltas that cancel, leaving a gamma/theta position."""
    b = Book()
    b.trade(atm(chain, "CE"), SELL, 65, 100.0)
    b.trade(atm(chain, "PE"), SELL, 65, 100.0)
    r = book_greeks(b.positions.values(), chains)
    net = r["net"]

    assert r["complete"] is True
    assert abs(net["delta"]) < 0.15 * 65          # legs cancel
    assert net["gamma"] < 0                        # short convexity
    assert net["theta"] > 0                        # collecting decay
    assert net["vega"] < 0                         # short vol
    assert "short gamma" in describe(net)


def test_long_and_short_of_the_same_contract_net_to_nothing(chain, chains):
    b = Book()
    ce = atm(chain, "CE")
    b.trade(ce, BUY, 65, 100.0)
    b.trade(ce, SELL, 65, 100.0)
    r = book_greeks(b.positions.values(), chains)
    assert all(abs(v) < 1e-9 for v in r["net"].values())


def test_futures_are_pure_delta(chain, chains):
    b = Book()
    fut = Instrument("NIFTY", "FUT", expiry=chain.expiry, lot_size=65)
    b.trade(fut, BUY, 65, 24_000.0)
    net = book_greeks(b.positions.values(), chains)["net"]
    assert net["delta"] == 65
    assert net["gamma"] == 0 and net["theta"] == 0 and net["vega"] == 0


def test_a_leg_with_no_chain_is_named_not_zero_filled(chain, chains):
    """A net delta that silently omits a leg is worse than no net delta."""
    b = Book()
    b.trade(atm(chain, "CE"), SELL, 65, 100.0)
    orphan = Instrument.option("BANKNIFTY", chain.expiry, 57_000.0, "PE", lot_size=35)
    b.trade(orphan, SELL, 35, 200.0)

    r = book_greeks(b.positions.values(), chains)
    assert r["complete"] is False
    assert r["unmarked"] == [orphan.label]
    assert "BANKNIFTY" not in r["by_underlying"]


def test_a_strike_outside_the_chain_is_unmarkable(chain, chains):
    b = Book()
    far = Instrument.option("NIFTY", chain.expiry, 999_999.0, "CE", lot_size=65)
    b.trade(far, SELL, 65, 1.0)
    r = book_greeks(b.positions.values(), chains)
    assert r["complete"] is False and r["unmarked"] == [far.label]


def test_breakdown_splits_by_underlying(chain, chains):
    b = Book()
    b.trade(atm(chain, "CE"), SELL, 65, 100.0)
    fut = Instrument("NIFTY", "FUT", expiry=chain.expiry, lot_size=65)
    b.trade(fut, BUY, 65, 24_000.0)
    r = book_greeks(b.positions.values(), chains)
    assert set(r["by_underlying"]) == {"NIFTY"}
    assert r["by_underlying"]["NIFTY"]["delta"] == pytest.approx(r["net"]["delta"])


def test_describe_reads_like_a_trader_speaks(chain, chains):
    b = Book()
    b.trade(atm(chain, "CE"), SELL, 65, 100.0)
    b.trade(atm(chain, "PE"), SELL, 65, 100.0)
    line = describe(book_greeks(b.positions.values(), chains)["net"])
    # A near-flat straddle reports its actual residual delta rather than
    # rounding to a friendly "flat" — a desk wants the number, not the label.
    assert "delta" in line
    assert "short gamma" in line
    assert "/day" in line          # theta stated as money per day
    assert "short vega" in line


def test_delta_flat_is_reserved_for_genuinely_flat():
    assert describe({"delta": 0.2, "gamma": 0, "theta": 0, "vega": 0, "rho": 0}) \
        .startswith("delta-flat")
    assert describe({"delta": -40.0, "gamma": 0, "theta": 0, "vega": 0, "rho": 0}) \
        .startswith("short 40 delta")

"""The short-side ledger — what the old long-only Portfolio could not express."""

from __future__ import annotations

from datetime import date

import pytest

from shunkan.portfolio.book import BUY, SELL, Book
from shunkan.portfolio.instrument import Instrument

EXP = date(2026, 8, 18)


def call(strike=24500.0):
    return Instrument.option("NIFTY", EXP, strike, "CE", lot_size=65)


def put(strike=24500.0):
    return Instrument.option("NIFTY", EXP, strike, "PE", lot_size=65)


# -- identity -----------------------------------------------------------------


def test_instrument_distinguishes_contracts_the_old_book_merged():
    keys = {call(24500).key, put(24500).key, call(24450).key,
            Instrument("NIFTY").key}
    assert len(keys) == 4  # all four were once the single bucket "NIFTY"


def test_instrument_round_trips_through_its_key():
    for inst in (call(), put(24450.5), Instrument("NIFTY", "FUT", expiry=EXP),
                 Instrument("RELIANCE")):
        assert Instrument.parse(inst.key) == Instrument(
            inst.symbol, inst.kind, inst.expiry, inst.strike)


def test_option_without_strike_or_expiry_is_rejected():
    with pytest.raises(ValueError):
        Instrument("NIFTY", "CE", expiry=EXP)       # no strike
    with pytest.raises(ValueError):
        Instrument("NIFTY", "CE", strike=24500.0)   # no expiry


# -- selling to open ----------------------------------------------------------


def test_sell_to_open_is_possible_at_all():
    b = Book()
    realized, closed, opened = b.trade(call(), SELL, 65, 139.70)
    assert opened == 65 and closed == 0 and realized == 0.0
    assert b.net_quantity(call()) == -65
    assert b.get(call()).is_short


def test_short_gains_when_premium_decays():
    b = Book()
    b.trade(call(), SELL, 65, 139.70)
    pos = b.get(call())
    assert pos.unrealized_pnl(100.0) == pytest.approx((139.70 - 100.0) * 65)
    assert pos.unrealized_pnl(200.0) == pytest.approx((139.70 - 200.0) * 65)


def test_short_position_is_a_negative_exposure():
    b = Book()
    b.trade(call(), SELL, 65, 139.70)
    assert b.market_value({call().key: 100.0}) == pytest.approx(-6500.0)


def test_short_straddle_is_representable():
    """The structure the old book could not hold at all."""
    b = Book()
    b.trade(call(), SELL, 65, 139.70)
    b.trade(put(), SELL, 65, 117.65)
    assert len(b.positions) == 2
    assert all(p.is_short for p in b.positions.values())
    credit = (139.70 + 117.65) * 65
    assert b.unrealized_pnl({call().key: 0.0, put().key: 0.0}) == pytest.approx(credit)


# -- offsetting ---------------------------------------------------------------


def test_sell_beyond_a_long_closes_it_then_goes_short():
    b = Book()
    b.trade(call(), BUY, 65, 100.0)
    realized, closed, opened = b.trade(call(), SELL, 130, 120.0)
    assert closed == 65 and opened == 65
    assert realized == pytest.approx((120.0 - 100.0) * 65)  # the long's profit
    assert b.net_quantity(call()) == -65                    # now net short


def test_buying_back_a_short_realizes_the_premium_captured():
    b = Book()
    b.trade(call(), SELL, 65, 139.70)
    realized, closed, opened = b.trade(call(), BUY, 65, 90.0)
    assert closed == 65 and opened == 0
    assert realized == pytest.approx((139.70 - 90.0) * 65)
    assert b.get(call()) is None  # flat positions leave the book


def test_offset_is_fifo_across_lots():
    b = Book()
    b.trade(call(), SELL, 65, 100.0)   # oldest
    b.trade(call(), SELL, 65, 200.0)
    realized, _, _ = b.trade(call(), BUY, 65, 150.0)
    assert realized == pytest.approx((100.0 - 150.0) * 65)  # oldest lot first
    assert b.net_quantity(call()) == -65


def test_avg_cost_tracks_the_open_side_only():
    b = Book()
    b.trade(call(), SELL, 65, 100.0)
    b.trade(call(), SELL, 65, 200.0)
    b.trade(call(), BUY, 65, 150.0)     # closes the 100 lot
    assert b.get(call()).avg_cost == pytest.approx(200.0)


def test_rejects_a_nonsense_side():
    with pytest.raises(ValueError):
        Book().trade(call(), "SHORT", 65, 100.0)


# -- expiry -------------------------------------------------------------------


def test_expired_contracts_are_surfaced_not_silently_settled():
    b = Book()
    dead = Instrument.option("NIFTY", date(2026, 8, 11), 24500.0, "CE", lot_size=65)
    b.trade(dead, SELL, 65, 10.0)
    b.trade(call(), SELL, 65, 139.70)
    expired = b.expired(when=date(2026, 8, 12))
    assert [p.instrument for p in expired] == [dead]
    assert len(b.positions) == 2  # nothing was auto-closed behind the trader


# -- multi-venue: a desk is not one exchange ----------------------------------


def test_same_underlying_on_two_venues_are_different_contracts():
    """SENSEX options list on BFO, NIFTY's on NFO. Keying without the venue
    would silently net a BSE position against an NSE one."""
    nfo = Instrument.option("SENSEX", EXP, 81000.0, "CE", exchange="NFO")
    bfo = Instrument.option("SENSEX", EXP, 81000.0, "CE", exchange="BFO")
    assert nfo.key != bfo.key
    b = Book()
    b.trade(nfo, SELL, 20, 100.0)
    b.trade(bfo, BUY, 20, 100.0)
    assert len(b.positions) == 2          # not netted to flat
    assert b.net_quantity(nfo) == -20
    assert b.net_quantity(bfo) == 20


def test_a_commodity_future_and_an_index_option_share_one_book():
    b = Book()
    crude = Instrument("CRUDEOIL", "FUT", expiry=EXP, lot_size=100, exchange="MCX")
    b.trade(crude, BUY, 100, 6200.0)
    b.trade(call(), SELL, 65, 139.70)
    venues = {p.instrument.exchange for p in b.positions.values()}
    assert venues == {"MCX", "NFO"}


def test_cash_and_derivative_venues_are_not_interchangeable():
    with pytest.raises(ValueError):
        Instrument("RELIANCE", exchange="MCX")           # equity on a deriv venue
    with pytest.raises(ValueError):
        Instrument("NIFTY", "CE", expiry=EXP, strike=1.0, exchange="NSE")


def test_derivatives_report_no_generic_quote_symbol():
    """A generic price source cannot resolve an exchange contract name, so it
    is told None rather than handed a symbol it would mis-resolve."""
    assert Instrument("RELIANCE").quote_symbol == "RELIANCE"
    assert call().quote_symbol is None

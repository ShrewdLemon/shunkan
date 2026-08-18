"""Economic multipliers for order-in-lots venues: sourced, never guessed."""

import pytest

from shunkan.data.contract_specs import (
    ORDER_IN_LOTS_VENUES,
    SPECS,
    economic_lot_size,
    kite_order_quantity,
    spec_for,
)
from shunkan.portfolio import Instrument


def test_every_spec_carries_its_verification():
    assert SPECS, "table must not be empty"
    for (ex, name), spec in SPECS.items():
        assert ex in ORDER_IN_LOTS_VENUES
        assert spec.multiplier >= 1
        assert spec.quote_unit and spec.deliverable
        # the provenance travels with the number, per the house rule
        assert "SPAN" in spec.margin_seen and "2026-08-18" in spec.margin_seen


def test_nfo_passes_the_dump_lot_through():
    assert economic_lot_size("NFO", "NIFTY", 75) == (75, "instruments dump")


def test_mcx_gold_resolves_with_source():
    lot, src = economic_lot_size("MCX", "GOLD", 1)
    assert lot == 100
    assert "1 kg" in src


def test_unknown_mcx_name_refuses_by_name():
    lot, src = economic_lot_size("MCX", "COTTONCNDY", 1)
    assert lot is None
    assert "COTTONCNDY" in src and "refused" in src


def test_changed_kite_convention_refuses_loudly():
    lot, src = economic_lot_size("MCX", "GOLD", 50)
    assert lot is None
    assert "50" in src and "convention" in src


def test_kite_order_quantity_flips_the_convention():
    from datetime import date

    gold = Instrument(symbol="GOLD", kind="FUT", expiry=date(2026, 10, 5),
                      lot_size=100, exchange="MCX")
    assert kite_order_quantity(gold, 200) == 2          # 2 lots
    nifty = Instrument(symbol="NIFTY", kind="FUT", expiry=date(2026, 8, 27),
                       lot_size=75, exchange="NFO")
    assert kite_order_quantity(nifty, 150) == 150       # units pass through
    with pytest.raises(ValueError, match="whole number of lots"):
        kite_order_quantity(gold, 250)                  # 2.5 lots: refused


def test_nickel_is_the_new_250_not_the_stale_1500():
    # MCX shrank the contract; stale references still say 1500 kg. The spec
    # table is only trustworthy if it tracks the exchange, not folklore.
    assert spec_for("MCX", "NICKEL").multiplier == 250

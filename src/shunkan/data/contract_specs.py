"""Economic contract multipliers for order-in-lots venues (MCX, CDS).

THE TRAP THIS MODULE EXISTS FOR: on NFO and BFO, Kite's instruments dump
carries the real lot size (one NIFTY lot = N units, price is per unit, so
price x quantity is rupees). On MCX and CDS the dump says lot_size=1 - Kite's
"order quantity is in lots" convention - while price is still quoted per unit
(GOLD in rupees per 10 grams, USDINR in rupees per dollar). Treat that 1 as an
economic lot and every rupee figure on those venues is wrong by the contract
multiplier: a 1 crore gold contract books as one lakh.

The multipliers below are quotation units per contract, from the exchanges'
published contract specifications (deliverable sizes cross-checked against
Zerodha's commodity margin page, fetched 2026-08-18, since MCX's own site
refuses non-browser fetches). Every entry was then verified against the
exchange the only way that cannot lie: Kite's SPAN basket calculator priced
1 lot of the front/second-month future on 2026-08-18, and margin divided by
(LTP x multiplier) had to land at a sane percentage. Results: GOLD and GOLDM
both 9.28%, all three silvers 12.85-12.86%, both natgas contracts 14.34%,
base metals 7.3-11.3%, currencies 2.4-4.0%, crude 30.85% on both months and
both contract sizes (the exchange's current elevated crude margin - the big
and mini contracts agreeing to three basis points is the multiplier proof;
the level is theirs). A multiplier off by even 2x would put those figures at
half or double the exchange's published band, and 10x is unmistakable.

House rule applies: a venue/name pair not in this table gets None and a
named reason, never a guess. NICKEL is 250 kg since MCX shrank the contract;
the old 1500 kg figure still circulating in stale references is exactly why
these numbers carry their verification with them.
"""

from __future__ import annotations

from dataclasses import dataclass

# Venues where Kite's dump lot_size=1 means "order in lots" and the economic
# multiplier must come from this table.
ORDER_IN_LOTS_VENUES = ("MCX", "CDS")

_ZERODHA_SRC = "deliverable per Zerodha margin calculator 2026-08-18"
_SPAN_SRC = "verified vs Kite SPAN basket 2026-08-18"


@dataclass(frozen=True)
class ContractSpec:
    name: str            # Kite instruments `name` column (GOLD, USDINR, ...)
    exchange: str
    multiplier: int      # quotation units per 1 lot; price x this = rupees/lot
    quote_unit: str      # what one price unit means
    deliverable: str     # the physical/notional size of one contract
    margin_seen: str     # the SPAN check result that verified this row


def _spec(name, exchange, multiplier, quote_unit, deliverable, margin_seen):
    return ContractSpec(name, exchange, multiplier, quote_unit, deliverable,
                        f"{margin_seen} ({_SPAN_SRC}; {_ZERODHA_SRC})")


SPECS: dict[tuple[str, str], ContractSpec] = {
    (s.exchange, s.name): s for s in [
        _spec("GOLD",       "MCX", 100,  "INR/10g",    "1 kg",       "9.28%"),
        _spec("GOLDM",      "MCX", 10,   "INR/10g",    "100 g",      "9.28%"),
        _spec("SILVER",     "MCX", 30,   "INR/kg",     "30 kg",      "12.85%"),
        _spec("SILVERM",    "MCX", 5,    "INR/kg",     "5 kg",       "12.86%"),
        _spec("SILVERMIC",  "MCX", 1,    "INR/kg",     "1 kg",       "12.85%"),
        _spec("CRUDEOIL",   "MCX", 100,  "INR/bbl",    "100 bbl",    "30.84% both months"),
        _spec("CRUDEOILM",  "MCX", 10,   "INR/bbl",    "10 bbl",     "30.84% both months"),
        _spec("NATURALGAS", "MCX", 1250, "INR/mmBtu",  "1250 mmBtu", "14.34%"),
        _spec("NATGASMINI", "MCX", 250,  "INR/mmBtu",  "250 mmBtu",  "14.34%"),
        _spec("COPPER",     "MCX", 2500, "INR/kg",     "2.5 t",      "9.28%"),
        _spec("ZINC",       "MCX", 5000, "INR/kg",     "5 t",        "9.26%"),
        _spec("ALUMINIUM",  "MCX", 5000, "INR/kg",     "5 t",        "9.23%"),
        _spec("LEAD",       "MCX", 5000, "INR/kg",     "5 t",        "7.32%"),
        _spec("NICKEL",     "MCX", 250,  "INR/kg",     "250 kg",     "11.26% second month"),
        _spec("USDINR",     "CDS", 1000, "INR/USD",    "USD 1,000",  "2.44%"),
        _spec("EURINR",     "CDS", 1000, "INR/EUR",    "EUR 1,000",  "2.63%"),
        _spec("GBPINR",     "CDS", 1000, "INR/GBP",    "GBP 1,000",  "3.05%"),
        _spec("JPYINR",     "CDS", 1000, "INR/100JPY", "JPY 100,000", "4.00%"),
    ]
}


def spec_for(exchange: str, name: str) -> ContractSpec | None:
    return SPECS.get((exchange.upper(), name.upper()))


def economic_lot_size(exchange: str, name: str,
                      dump_lot: int | None) -> tuple[int | None, str]:
    """The lot size that makes price x quantity rupees, with its source.

    NFO/BFO: the dump's lot IS economic - pass it through. MCX/CDS: the dump
    must say 1 (the order-in-lots convention this table assumes); a different
    value means Kite changed conventions and mixing them silently would be
    worse than refusing, so refuse with the observation named.
    """
    ex = exchange.upper()
    if ex not in ORDER_IN_LOTS_VENUES:
        return dump_lot, ("instruments dump" if dump_lot
                          else "no lot in the instruments dump")
    spec = spec_for(ex, name)
    if spec is None:
        return None, (f"no sourced economic multiplier for {ex}:{name.upper()} "
                      "- rupee math refused rather than guessed")
    if dump_lot not in (None, 1):
        return None, (f"Kite reports lot_size={dump_lot} on {ex} - the "
                      "order-in-lots convention this table assumes has "
                      "changed; refusing until re-verified")
    return spec.multiplier, f"{ex} contract spec: {spec.deliverable} ({spec.margin_seen})"


def kite_order_quantity(instrument, economic_quantity: float) -> int:
    """Convert book (economic) quantity to what Kite's API wants.

    NFO/BFO orders are placed in units; MCX/CDS orders in LOTS. The book
    always stores economic units so rupee math is uniform, which makes this
    boundary the one place the convention flips back. Refuses a quantity
    that is not a whole number of lots - the exchange would too.
    """
    qty = abs(economic_quantity)
    if instrument.exchange not in ORDER_IN_LOTS_VENUES:
        return int(qty)
    spec = spec_for(instrument.exchange, instrument.symbol)
    if spec is None:
        raise ValueError(
            f"no economic multiplier for {instrument.exchange}:{instrument.symbol} "
            "- cannot convert to an order quantity")
    lots = qty / spec.multiplier
    if abs(lots - round(lots)) > 1e-9 or round(lots) < 1:
        raise ValueError(
            f"{qty:g} units of {instrument.label} is not a whole number of "
            f"lots (x{spec.multiplier}) - the exchange trades whole lots only")
    return int(round(lots))

"""What a position is *in*.

The old book keyed positions on a bare symbol string, so a 24500 call, a
24450 put and the index itself all collapsed into one bucket called NIFTY —
and next week's series merged into this week's. An F&O book needs the
contract's full identity or it cannot mark, expire or net anything.

Instruments are frozen and hashable so they key the positions dict directly,
and round-trip through a single stable string for persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

EQ = "EQ"
FUT = "FUT"
CE = "CE"
PE = "PE"
OPTION_KINDS = (CE, PE)

# Venues a desk actually trades. The same underlying can list on more than one
# — SENSEX options are BFO, NIFTY's are NFO, and CRUDEOIL is MCX — so the
# venue is part of a contract's identity, not a lookup detail.
CASH_EXCHANGES = ("NSE", "BSE")
DERIV_EXCHANGES = ("NFO", "BFO", "MCX", "CDS")
EXCHANGES = CASH_EXCHANGES + DERIV_EXCHANGES


@dataclass(frozen=True)
class Instrument:
    """One tradeable contract.

    `lot_size` is carried but never guessed — None means no source could name
    it, and every rupee figure downstream must show per-unit money and say so
    rather than multiply by an assumption.
    """

    symbol: str                      # underlying, e.g. NIFTY, CRUDEOIL, RELIANCE
    kind: str = EQ                   # EQ | FUT | CE | PE
    expiry: date | None = None
    strike: float | None = None
    lot_size: int | None = None
    exchange: str = ""               # blank means the usual venue for this kind

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "kind", self.kind.upper())
        if self.kind not in (EQ, FUT, CE, PE):
            raise ValueError(f"unknown instrument kind {self.kind!r}")
        # Default to the Indian equity venues, which is what every position
        # written before exchanges existed already meant.
        exchange = (self.exchange or ("NSE" if self.kind == EQ else "NFO")).upper()
        if exchange not in EXCHANGES:
            raise ValueError(f"unknown exchange {exchange!r}")
        if self.kind == EQ and exchange not in CASH_EXCHANGES:
            raise ValueError(f"{exchange} is a derivatives venue; EQ cannot list there")
        if self.kind != EQ and exchange not in DERIV_EXCHANGES:
            raise ValueError(f"{exchange} is a cash venue; {self.kind} cannot list there")
        object.__setattr__(self, "exchange", exchange)
        if self.kind in OPTION_KINDS:
            if self.expiry is None or self.strike is None:
                raise ValueError(f"{self.kind} needs both an expiry and a strike")
        if self.kind == FUT and self.expiry is None:
            raise ValueError("FUT needs an expiry")
        if self.kind == EQ and (self.expiry is not None or self.strike is not None):
            raise ValueError("EQ has no expiry or strike")

    @property
    def is_option(self) -> bool:
        return self.kind in OPTION_KINDS

    @property
    def derivative(self) -> bool:
        return self.kind != EQ

    @property
    def key(self) -> str:
        """Stable identity — the positions dict key and the persisted name."""
        if self.kind == EQ:
            return f"{self.exchange}:{self.symbol}"
        if self.kind == FUT:
            return f"{self.exchange}:{self.symbol}|{self.expiry:%Y-%m-%d}|FUT"
        return (f"{self.exchange}:{self.symbol}|{self.expiry:%Y-%m-%d}"
                f"|{self.strike:g}|{self.kind}")

    @property
    def label(self) -> str:
        """How a trader says it out loud: 'NIFTY 18AUG26 24500 CE'."""
        if self.kind == EQ:
            return self.symbol
        when = f"{self.expiry:%d%b%y}".upper()
        if self.kind == FUT:
            return f"{self.symbol} {when} FUT"
        return f"{self.symbol} {when} {self.strike:g} {self.kind}"

    @property
    def quote_symbol(self) -> str | None:
        """What a price source should be asked for, or None if it cannot.

        Cash instruments quote under their plain name. Derivatives quote under
        an exchange contract name that only the broker's instruments dump
        knows, so a generic provider must be told it has nothing to offer
        rather than handed a symbol it will silently mis-resolve.
        """
        return self.symbol if self.kind == EQ else None

    def expired(self, when: date | None = None) -> bool:
        """True once the contract's expiry date has passed.

        Intraday precision lives in markets.is_expired; this is the date-level
        question the book asks when deciding what still belongs in it.
        """
        if self.expiry is None:
            return False
        from shunkan.markets import today_ist

        return self.expiry < (when or today_ist())

    def settled(self, when=None) -> bool:
        """True once this contract's 15:30 IST bell has rung.

        Stricter in time than `expired`, which is the date-level question the
        book asks about membership. The two differ only between 15:30 and
        midnight on expiry day — and that window is exactly when a settlement
        price first becomes knowable, so it is this predicate, not `expired`,
        that gates recording one.

        `when` is a DATETIME here, not the date `expired` takes: this question
        is the intraday one, and markets.is_expired is the single clock that
        answers it.
        """
        if self.expiry is None:
            return False
        from shunkan.markets import is_expired

        return is_expired(self.expiry, when)

    @classmethod
    def parse(cls, key: str, lot_size: int | None = None) -> "Instrument":
        """Inverse of `key`. Round-trips persisted positions."""
        # Keys written before exchanges existed carry no venue prefix. They
        # were all NSE/NFO, so defaulting them is a correct reading of the old
        # model, not a guess about it.
        exchange = ""
        if ":" in key.split("|")[0]:
            exchange, key = key.split(":", 1)
        parts = key.split("|")
        if len(parts) == 1:
            return cls(symbol=parts[0], kind=EQ, lot_size=lot_size,
                       exchange=exchange)
        if len(parts) == 3 and parts[2].upper() == FUT:
            return cls(symbol=parts[0], kind=FUT,
                       expiry=date.fromisoformat(parts[1]), lot_size=lot_size,
                       exchange=exchange)
        if len(parts) == 4:
            return cls(symbol=parts[0], kind=parts[3],
                       expiry=date.fromisoformat(parts[1]),
                       strike=float(parts[2]), lot_size=lot_size,
                       exchange=exchange)
        raise ValueError(f"unparseable instrument key {key!r}")

    @classmethod
    def option(cls, symbol: str, expiry: date, strike: float, right: str,
               lot_size: int | None = None, exchange: str = "") -> "Instrument":
        """Build a leg straight off a chain row, where all of this is known."""
        return cls(symbol=symbol, kind=right, expiry=expiry, strike=strike,
                   lot_size=lot_size, exchange=exchange)

"""Paper book: cash, margin and a sided position ledger.

Cash moves the way a broker's does. Buying spends it. Selling to open
*credits* premium and posts margin instead — so a short's cash line goes up
while its risk goes up too, which is exactly the asymmetry the old long-only
model could not represent.

Margin is reported, never enforced and never guessed: when the broker can
price it we show its number, and when it cannot we show nothing at all. A
plausible-looking estimate is the same class of error as a guessed lot size.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from shunkan.config import PORTFOLIO_FILE, ensure_dirs
from shunkan.portfolio.book import BUY, SELL, Book, BookPosition, Lot
from shunkan.portfolio.instrument import Instrument

__all__ = ["Portfolio", "Book", "BookPosition", "Lot", "Instrument", "BUY", "SELL"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Portfolio:
    def __init__(self, cash: float = 100_000.0, path: Path | None = None) -> None:
        self.cash = cash
        self.book = Book()
        self.realized_pnl = 0.0
        self.history: list[dict] = []  # trade journal
        self.path = path or PORTFOLIO_FILE
        # Last exchange-priced margin for the WHOLE book. Margin does not
        # decompose per leg — SPAN nets offsetting risk across the basket, so
        # an iron condor costs a third of one naked short. A per-leg number
        # would be arithmetic the exchange never agreed to.
        self.margin: dict | None = None
        # The book we last ASKED the exchange about, priced or refused. The
        # answer only ever applied to that exact basket, so a second ask about
        # it is spent broker latency for a number already held — this is what
        # lets a view poll price_margin() without costing a round trip a poll.
        self.margin_asked: list | None = None
        # Why the last ask produced no number. When there is no figure to show,
        # the reason IS the product: the tile owes the trader a dash AND a why.
        self.margin_error: str | None = None

    @property
    def positions(self) -> dict[str, BookPosition]:
        return self.book.positions

    # -- trading ----------------------------------------------------------

    def trade(self, instrument: Instrument | str, side: str, quantity: float,
              price: float) -> float:
        """Record one fill. Returns realized P&L.

        A single fill can both close and open (sell 2 while long 1), so cash
        is applied to each part separately: the closed portion settles at the
        traded price, the opened portion spends or receives premium.
        """
        inst = self._coerce(instrument)
        side = side.upper()
        realized, closed, opened = self.book.trade(inst, side, quantity, price)

        # Buying pays out, selling takes in — for both closing and opening.
        self.cash += (quantity * price) * (-1 if side == BUY else 1)
        self.realized_pnl += realized
        self._journal(side, inst, quantity, price,
                      realized=realized if closed else None,
                      closed=closed or None, opened=opened or None)
        return realized

    def trade_lots(self, instrument: Instrument, side: str, lots: int,
                   price: float) -> float:
        """Trade in lots, the unit F&O is actually quoted in.

        Refuses when the lot size is unknown rather than assuming one — the
        whole position size would otherwise be silently wrong.
        """
        if not instrument.lot_size:
            raise ValueError(
                f"No lot size for {instrument.label} — cannot size in lots. "
                "Trade in units, or reconnect a source that names the lot."
            )
        return self.trade(instrument, side, lots * instrument.lot_size, price)

    def settle_expired(self, instrument: Instrument | str, price: float) -> float:
        """Close a dead contract at a settlement price the TRADER supplies.

        Expiry is the one cash event this book cannot observe. There is no
        settlement feed here, and deriving a price from whatever spot happened
        to be cached would write a fabricated number straight into realized
        P&L — the same class of error as a guessed lot size. So the trader
        states it, including the 0 of an option that expired worthless, which
        is typed rather than assumed on their behalf.

        The offset itself is an ordinary closing fill: same FIFO, same realized
        arithmetic, same cash direction. Only the journal differs, and that is
        the point — the history has to say which cash movements were executed
        and which were asserted.
        """
        inst = self._coerce(instrument)
        pos = self.book.get(inst)
        if pos is None or not pos.net_quantity:
            raise ValueError(f"No open position in {inst.label} to settle.")
        if not inst.settled():
            # Gated on the bell, not the date: a contract that still trades is
            # closed with a trade, at a price the market actually printed.
            raise ValueError(
                f"{inst.label} has not stopped trading — close it with a trade, "
                "not a settlement."
            )
        if not (price >= 0):  # NaN fails this too, which is the intent
            raise ValueError("settlement price must be zero or positive")

        quantity = abs(pos.net_quantity)
        side = SELL if pos.net_quantity > 0 else BUY
        realized, closed, _ = self.book.trade(inst, side, quantity, price)
        self.cash += (quantity * price) * (-1 if side == BUY else 1)
        self.realized_pnl += realized
        self._journal(
            side, inst, quantity, price, realized=realized, closed=closed,
            settlement=True,
            note="settled at a price the trader supplied — not an executed fill")
        return realized

    # Long-only helpers kept so existing equity callers and the CLI keep working.
    def buy(self, symbol: str, quantity: float, price: float) -> None:
        inst = self._coerce(symbol)
        cost = quantity * price
        if cost > self.cash + 1e-9:
            raise ValueError(
                f"Insufficient cash: need Rs {cost:,.2f}, have Rs {self.cash:,.2f}"
            )
        self.trade(inst, BUY, quantity, price)

    def sell(self, symbol: str, quantity: float, price: float) -> float:
        """Sell. Unlike the old book this may open a short rather than raise."""
        return self.trade(self._coerce(symbol), SELL, quantity, price)

    @staticmethod
    def _coerce(instrument: Instrument | str) -> Instrument:
        if isinstance(instrument, Instrument):
            return instrument
        return Instrument.parse(str(instrument).upper())

    # -- valuation ----------------------------------------------------------

    def market_value(self, prices: dict[str, float]) -> float:
        return self.book.market_value(prices)

    def total_equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        return self.book.unrealized_pnl(prices)

    def margin_used(self) -> float | None:
        """Exchange-priced margin for the book, or None when it is unknown.

        Deliberately all-or-nothing. A total computed while some leg could not
        be priced would read as complete and understate risk, and there is no
        local approximation worth showing beside it — the netting that makes a
        condor cheaper than one naked short is the exchange's arithmetic.
        """
        if not self.positions:
            return 0.0
        if not self.margin or self.margin.get("unpriceable"):
            return None
        if self.margin.get("book") != self._book_fingerprint():
            return None  # priced against a different book, so currently unknown
        return self.margin["final"]["total"]

    def margin_status(self) -> dict:
        """Why margin_used() reads what it reads, in words a desk can act on.

        A dash is only honest if it can name its own cause: nothing to margin,
        never asked, asked and refused, priced against a book that has since
        changed, or priced but missing a leg the exchange would not name. The
        branches mirror margin_used() exactly and must never disagree with it.
        """
        if not self.positions:
            return {"state": "flat", "reason": "no positions — nothing to margin"}
        if not self.margin:
            if self.margin_asked == self._book_fingerprint():
                return {"state": "refused",
                        "reason": self.margin_error
                        or "the exchange did not price this book"}
            return {"state": "unpriced",
                    "reason": "not priced — SPAN nets across the whole basket, "
                              "so it is one exchange call for the book, never a "
                              "sum of legs"}
        if self.margin.get("unpriceable"):
            return {"state": "unpriceable",
                    "reason": "no exchange contract name for "
                              + " · ".join(self.margin["unpriceable"])}
        if self.margin.get("book") != self._book_fingerprint():
            return {"state": "stale",
                    "reason": "priced against a different book — the book has "
                              "changed since"}
        return {"state": "priced", "reason": self.margin.get("source", "")}

    def _book_fingerprint(self) -> list[tuple[str, float]]:
        """Identity AND size. Halving a leg leaves the position keys
        untouched but changes the margin completely, so a key-only
        fingerprint would keep reporting a number the exchange never quoted."""
        return sorted((k, p.net_quantity) for k, p in self.positions.items())

    def price_margin(self, kite, force: bool = False) -> dict | None:
        """Ask the exchange what this book costs to hold.

        Never raises: an unpriceable book shows a dash, it does not break the
        portfolio. Prices the NET position per instrument, which is what the
        exchange actually margins.

        Idempotent per book state, and that idempotence IS the trigger design.
        The answer is only ever valid for the exact basket it was asked about
        (see margin_used), so a second ask about that basket spends broker
        latency to re-learn a number already held. A caller may therefore ask
        on every draw and a desk that adjusts all day still pays exactly one
        round trip per adjustment — which is why nothing prices margin per
        FILL: a four-leg condor is entered one leg at a time, and three of
        those four baskets are books the trader never meant to hold. `force`
        is the trader's explicit re-ask, the only way past the memo — and the
        only way to pick up SPAN parameters that moved under an unchanged book.
        """
        from shunkan.data.kite_fno import basket_margin

        if not self.positions:
            self.margin = self.margin_error = self.margin_asked = None
            return None
        fingerprint = self._book_fingerprint()
        if not force and self.margin_asked == fingerprint:
            return self.margin  # this exact basket has already been asked about
        self.margin_asked = fingerprint
        legs = [{"instrument": p.instrument,
                 "side": SELL if p.net_quantity < 0 else BUY,
                 "quantity": abs(p.net_quantity)}
                for p in self.positions.values() if p.net_quantity]
        try:
            priced = basket_margin(kite, legs)
        except Exception as exc:
            # Still no number, and still no estimate — but now the refusal can
            # say what was tried, which is the difference between an honest
            # dash and a blank one.
            self.margin = None
            self.margin_error = str(exc)[:200]
            return None
        priced["book"] = fingerprint
        self.margin, self.margin_error = priced, None
        return priced

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        ensure_dirs()
        data = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "positions": {
                key: {
                    "lot_size": pos.instrument.lot_size,
                    "lots": [
                        {"side": l.side, "quantity": l.quantity,
                         "price": l.price, "timestamp": l.timestamp}
                        for l in pos.lots
                    ],
                }
                for key, pos in self.positions.items()
            },
            "history": self.history[-500:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path | None = None) -> "Portfolio":
        path = path or PORTFOLIO_FILE
        pf = cls(path=path)
        if not path.exists():
            return pf
        try:
            data = json.loads(path.read_text())
            pf.cash = float(data.get("cash", pf.cash))
            pf.realized_pnl = float(data.get("realized_pnl", 0.0))
            pf.history = list(data.get("history", []))
            for key, saved in data.get("positions", {}).items():
                # Pre-book files stored a bare list of lots with no side. That
                # model was long-only, so BUY is the correct reading, not a guess.
                raw = saved if isinstance(saved, list) else saved.get("lots", [])
                lot_size = None if isinstance(saved, list) else saved.get("lot_size")
                inst = Instrument.parse(key, lot_size=lot_size)
                pf.book.positions[key] = BookPosition(
                    instrument=inst,
                    lots=[Lot(side=l.get("side", BUY), quantity=float(l["quantity"]),
                              price=float(l["price"]), timestamp=l.get("timestamp", ""))
                          for l in raw],
                )
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, OSError):
            return cls(path=path)  # corrupted file: start fresh rather than crash
        return pf

    def _journal(self, side: str, inst: Instrument, qty: float, price: float,
                 **extra) -> None:
        self.history.append({
            "time": _now(), "side": side, "symbol": inst.key,
            "label": inst.label, "qty": qty, "price": price,
            **{k: v for k, v in extra.items() if v is not None},
        })

"""Which columns a table may show, and which it shows by default.

This registry is the SERVER's half of a pair. The other half is `TABLE_COLS`
in src/shunkan/server/static/app.js, which owns the cell renderers. The two
are pinned together by
tests/test_column_views.py::test_the_two_column_registries_agree, because this
repo has shipped the same shape of bug twice: a producer and a consumer that
disagree about one list, where both halves exist, nothing errors, and the data
is simply absent (git 39df03b, a45f6d7). A column id here that no renderer
knows would come back off disk as a permanently empty column.

A saved view is therefore VALIDATED against this registry, not stored
verbatim the way /api/layout stores a workspace blob. The layout store was
rejected for column views for three reasons, all of which would have shipped a
control that cannot succeed:

  * its key space is flat and shared — a column view saved there appears in
    the workspace layout dropdown as a selectable workspace;
  * DELETE /api/layout refuses to remove the last key, which is the wrong rule
    for a set of column views (deleting your only one must work);
  * layouts have no table dimension, and a chain view is meaningless on the
    screener.

So this is a sibling store on the pulse_boards.json pattern: one JSON file
under APP_DIR, whole-file rewrite, and a POST that REFUSES junk rather than
persisting it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A view name is the user's own text. It is echoed back into HTML by the
# frontend and used as a JSON key here, so it is narrowed at the door rather
# than escaped at every use site. Same character class as the workspace
# layout picker, plus spaces: "risk desk" is a name a desk would actually type.
_NAME_OK = re.compile(r"[^a-z0-9 _-]+")
NAME_MAX = 32

# "default" names the built-in set, which is not stored. Letting a user save a
# view under that name would shadow it with no way back to today's columns.
RESERVED_NAMES = frozenset({"default"})


@dataclass(frozen=True)
class Table:
    """One table kind: its columns in RENDER order, and the identity column.

    `locked` is the column that answers "which row is this?". A view without
    it is a table of numbers with nothing to attach them to, so it is refused
    rather than silently repaired — a repaired view would disagree with what
    the user's picker showed them.
    """

    title: str
    locked: str
    columns: tuple[tuple[str, str], ...]      # (id, label), render order
    default: tuple[str, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(c for c, _ in self.columns)


TABLES: dict[str, Table] = {
    "chain": Table(
        title="OPTION CHAIN — STRIKES",
        locked="strike",
        columns=(
            ("call_oi", "C·OI"), ("call_doi", "C·ΔOI"), ("call_vol", "C·VOL"),
            ("call_iv", "C·IV"), ("call_bid", "C·BID"), ("call_ltp", "C·LTP"),
            ("strike", "STRIKE"),
            ("put_ltp", "P·LTP"), ("put_ask", "P·ASK"), ("put_iv", "P·IV"),
            ("put_vol", "P·VOL"), ("put_doi", "P·ΔOI"), ("put_oi", "P·OI"),
        ),
        # Every field /api/chain/{symbol} carries per strike, and nothing else:
        # the payload has no unused column to offer.
        default=(
            "call_oi", "call_doi", "call_vol", "call_iv", "call_bid", "call_ltp",
            "strike",
            "put_ltp", "put_ask", "put_iv", "put_vol", "put_doi", "put_oi",
        ),
    ),
    "screener": Table(
        title="SCREENER",
        locked="symbol",
        columns=(
            ("symbol", "SYMBOL"), ("price", "PRICE"), ("ret_1d", "1D"),
            ("ret_1w", "1W"), ("ret_1mo", "1M"), ("ret_3mo", "3M"),
            ("rsi", "RSI"), ("vol_ann", "VOL ANN"), ("vol_surge", "VOL SURGE"),
            ("from_high", "OFF HIGH"), ("above_sma50", "SMA50"),
            ("above_sma200", "SMA200"),
        ),
        # ret_1d and vol_surge are computed by run_screen and returned by
        # /api/screen already; before saved views they were transmitted and
        # dropped on the floor. They are OFF by default so the shipped table
        # is unchanged for anyone who never opens the picker.
        default=(
            "symbol", "price", "ret_1w", "ret_1mo", "ret_3mo", "rsi",
            "vol_ann", "from_high", "above_sma50", "above_sma200",
        ),
    ),
    "positions": Table(
        title="PORTFOLIO — POSITIONS",
        locked="contract",
        columns=(
            ("contract", "CONTRACT"), ("kind", "KIND"), ("expiry", "EXPIRY"),
            ("strike", "STRIKE"), ("qty", "QTY"), ("lots", "LOTS"),
            ("avg", "AVG"), ("last", "LAST"), ("value", "VALUE"),
            ("pnl", "P&L"),
        ),
        default=("contract", "qty", "avg", "last", "value", "pnl"),
    ),
    "insider": Table(
        title="INSIDER DEALING — PIT REG 7",
        locked="person",
        columns=(
            ("date", "DATE"), ("person", "PERSON"), ("relation", "RELATION"),
            ("deal", "DEAL"), ("mode", "MODE"), ("security", "SECURITY"),
            ("qty", "QTY"), ("value", "VALUE"),
            ("shares", "SHARES BEFORE→AFTER"), ("stake", "STAKE"),
        ),
        default=("date", "person", "relation", "deal", "qty", "value",
                 "shares", "stake"),
    ),
    "fund_holdings": Table(
        title="SCHEME — DISCLOSED HOLDINGS",
        locked="holding",
        columns=(
            ("holding", "HOLDING"), ("symbol", "SYMBOL"), ("sector", "SECTOR"),
            ("value_cr", "₹Cr"), ("weight", "WEIGHT"), ("chg_1m", "Δ1M"),
            ("as_of", "AS OF"),
        ),
        default=("holding", "symbol", "sector", "value_cr", "weight", "chg_1m"),
    ),
    "stock_holders": Table(
        title="SCHEMES HOLDING A STOCK",
        locked="scheme",
        columns=(
            ("scheme", "SCHEME"), ("amc", "AMC"), ("category", "CATEGORY"),
            ("weight", "WEIGHT"), ("value_cr", "₹Cr"), ("chg_1m", "Δ1M"),
        ),
        default=("scheme", "amc", "category", "weight", "value_cr", "chg_1m"),
    ),
}


def known_tables() -> list[str]:
    return sorted(TABLES)


def get_table(kind: str) -> Table:
    """The table, or a KeyError whose message NAMES what exists.

    "unknown table" on its own reads as a defect and gets 'fixed' by
    loosening the check until it accepts anything.
    """
    try:
        return TABLES[kind]
    except KeyError:
        raise KeyError(
            f"no column view for table {kind!r} — Shunkan defines column "
            f"views for: {', '.join(known_tables())}"
        ) from None


def clean_name(name: str) -> str:
    """Narrow a user-typed view name, or refuse it by name."""
    raw = str(name or "")
    cleaned = _NAME_OK.sub("", raw.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValueError(
            f"view name {raw!r} has no usable characters — names may use "
            "a-z, 0-9, space, hyphen and underscore"
        )
    if len(cleaned) > NAME_MAX:
        raise ValueError(
            f"view name is {len(cleaned)} characters; the limit is {NAME_MAX}"
        )
    if cleaned in RESERVED_NAMES:
        raise ValueError(
            f"{cleaned!r} is the built-in view showing the shipped columns — "
            "pick another name so you can still get back to it"
        )
    return cleaned


def validate(kind: str, columns) -> list[str]:
    """Return `columns` in RENDER order, or raise ValueError naming the cause.

    Order comes from the registry rather than from the user's click order on
    purpose: the chain reads calls | strike | puts, and a view that put P·OI
    left of the strike would not be a preference, it would be unreadable.
    Saying so is this function's job; the picker says it on screen too.
    """
    table = get_table(kind)
    if not isinstance(columns, list):
        raise ValueError("columns must be a list of column ids")
    want: list[str] = []
    for c in columns:
        if not isinstance(c, str):
            raise ValueError(f"column id must be a string, got {type(c).__name__}")
        if c not in table.ids:
            raise ValueError(
                f"{kind}: no column {c!r} — {kind} has "
                f"{', '.join(table.ids)}"
            )
        if c not in want:
            want.append(c)
    if not want:
        raise ValueError(f"{kind}: a view must keep at least one column")
    if table.locked not in want:
        raise ValueError(
            f"{kind}: {table.locked!r} identifies the row and cannot be "
            "dropped — without it the numbers belong to nothing"
        )
    return [c for c in table.ids if c in want]


def describe(kind: str) -> dict:
    """The registry half of what GET /api/views returns."""
    t = get_table(kind)
    return {
        "table": kind,
        "title": t.title,
        "locked": t.locked,
        "columns": [{"id": i, "label": lb} for i, lb in t.columns],
        "default": list(t.default),
    }

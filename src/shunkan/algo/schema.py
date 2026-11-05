"""On-disk format for recorded depth ticks.

Flat columns rather than nested lists: Parquet's dictionary + delta encodings
compress repeated price levels hard, and a flat frame loads straight into
numpy for a replay loop without per-row object churn. Five levels a side is
everything Kite publishes.

One row per tick per instrument. A busy NIFTY option session is a few million
rows; at roughly 20 bytes/row compressed that is well under a gigabyte a day.
"""

from __future__ import annotations

import pyarrow as pa

from shunkan.stream.parser import DEPTH_LEVELS, Tick

SCHEMA_VERSION = 1

_BASE = [
    ("ts_ns", pa.int64()),          # local receive time, monotonic-ish wall clock
    ("exchange_ts", pa.int32()),    # exchange clock, epoch seconds
    ("token", pa.int32()),
    ("ltp", pa.float64()),
    ("last_qty", pa.int32()),
    ("atp", pa.float64()),
    ("volume", pa.int64()),
    ("oi", pa.int64()),
    ("total_buy_qty", pa.int64()),
    ("total_sell_qty", pa.int64()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
]


def _depth_fields() -> list[tuple[str, pa.DataType]]:
    out: list[tuple[str, pa.DataType]] = []
    for side in ("bid", "ask"):
        for i in range(DEPTH_LEVELS):
            out.append((f"{side}_px_{i}", pa.float64()))
            out.append((f"{side}_qty_{i}", pa.int32()))
            out.append((f"{side}_ord_{i}", pa.int32()))
    return out


COLUMNS: list[tuple[str, pa.DataType]] = _BASE + _depth_fields()
COLUMN_NAMES: list[str] = [n for n, _ in COLUMNS]
ARROW_SCHEMA = pa.schema(
    [pa.field(n, t) for n, t in COLUMNS],
    metadata={b"shunkan_schema_version": str(SCHEMA_VERSION).encode()},
)


def tick_to_row(tick: Tick, ts_ns: int) -> dict:
    """Flatten one tick. Absent depth levels are written as 0.0/0, which
    `replay` reads back as "no level" rather than as a real price of zero."""
    row: dict = {
        "ts_ns": ts_ns,
        "exchange_ts": tick.exchange_ts,
        "token": tick.token,
        "ltp": tick.ltp,
        "last_qty": tick.last_qty,
        "atp": tick.atp,
        "volume": tick.volume,
        "oi": tick.oi,
        "total_buy_qty": tick.total_buy_qty,
        "total_sell_qty": tick.total_sell_qty,
        "open": tick.open,
        "high": tick.high,
        "low": tick.low,
        "close": tick.close,
    }
    for side, levels in (("bid", tick.bids), ("ask", tick.asks)):
        for i in range(DEPTH_LEVELS):
            if i < len(levels):
                row[f"{side}_px_{i}"] = levels[i].price
                row[f"{side}_qty_{i}"] = levels[i].quantity
                row[f"{side}_ord_{i}"] = levels[i].orders
            else:
                row[f"{side}_px_{i}"] = 0.0
                row[f"{side}_qty_{i}"] = 0
                row[f"{side}_ord_{i}"] = 0
    return row

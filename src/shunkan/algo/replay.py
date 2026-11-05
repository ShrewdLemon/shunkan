"""Read recorded depth back as a deterministic tick stream.

Replay is the contract between research and production: the same strategy
object consumes ticks here and from the live socket, so a result that cannot
be reproduced by replay is a bug in the strategy, not in the market.

Ordering is by `ts_ns`, the local receive stamp, not the exchange stamp.
Exchange timestamps are second-resolution and arrive out of order across
instruments; sorting by them would silently reorder events inside a second
and let a strategy see a book update before the trade that caused it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pyarrow.parquet as pq

from shunkan.algo.recorder import TICKS_DIR
from shunkan.stream.parser import DEPTH_LEVELS, DepthLevel, Tick


@dataclass(frozen=True)
class Event:
    """A recorded tick plus the receive time it was stamped with."""

    ts_ns: int
    tick: Tick

    @property
    def ts(self) -> float:
        return self.ts_ns / 1e9


def session_parts(session: str, root: Path | None = None) -> list[Path]:
    root = Path(root) if root else TICKS_DIR
    d = root / session
    if not d.is_dir():
        raise FileNotFoundError(f"no recording at {d}")
    return sorted(d.glob("part-*.parquet"))


def session_meta(session: str, root: Path | None = None) -> dict:
    root = Path(root) if root else TICKS_DIR
    p = root / f"{session}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _rebuild(cols: dict, i: int) -> Tick:
    bids: list[DepthLevel] = []
    asks: list[DepthLevel] = []
    for side, out in (("bid", bids), ("ask", asks)):
        for lv in range(DEPTH_LEVELS):
            px = cols[f"{side}_px_{lv}"][i]
            if px <= 0:
                break  # levels are packed best-first; a zero ends the side
            out.append(
                DepthLevel(
                    price=px,
                    quantity=int(cols[f"{side}_qty_{lv}"][i]),
                    orders=int(cols[f"{side}_ord_{lv}"][i]),
                )
            )
    return Tick(
        token=int(cols["token"][i]),
        ltp=cols["ltp"][i],
        mode="full",
        volume=int(cols["volume"][i]),
        oi=int(cols["oi"][i]),
        open=cols["open"][i],
        high=cols["high"][i],
        low=cols["low"][i],
        close=cols["close"][i],
        last_qty=int(cols["last_qty"][i]),
        atp=cols["atp"][i],
        total_buy_qty=int(cols["total_buy_qty"][i]),
        total_sell_qty=int(cols["total_sell_qty"][i]),
        exchange_ts=int(cols["exchange_ts"][i]),
        bids=bids,
        asks=asks,
    )


def replay(
    session: str,
    root: Path | None = None,
    tokens: list[int] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> Iterator[Event]:
    """Yield recorded ticks in receive order.

    `tokens` filters instruments; `start_ns`/`end_ns` window the session --
    useful for isolating a regime, e.g. the 15:15-15:40 window where the cash
    index is frozen but derivatives keep trading.
    """
    want = set(tokens) if tokens else None
    rows: list[tuple[int, dict, int]] = []
    for part in session_parts(session, root):
        table = pq.read_table(part)
        cols = {name: table.column(name).to_pylist() for name in table.column_names}
        ts = cols["ts_ns"]
        tok = cols["token"]
        for i in range(len(ts)):
            if want is not None and tok[i] not in want:
                continue
            if start_ns is not None and ts[i] < start_ns:
                continue
            if end_ns is not None and ts[i] > end_ns:
                continue
            rows.append((ts[i], cols, i))
    # Parts are written in order, but a stable sort makes the guarantee
    # explicit and survives someone concatenating recordings by hand.
    rows.sort(key=lambda r: r[0])
    for ts_ns, cols, i in rows:
        yield Event(ts_ns=ts_ns, tick=_rebuild(cols, i))


def replay_frame(session: str, root: Path | None = None):
    """The whole recording as a pandas DataFrame, for exploratory work.

    Not what the engine uses -- the engine streams events so it can never
    peek forward. This is for looking at a session by eye.
    """
    import pandas as pd

    parts = session_parts(session, root)
    if not parts:
        return pd.DataFrame()
    df = pq.read_table(parts[0]).to_pandas() if len(parts) == 1 else pq.read_table(
        parts[0].parent
    ).to_pandas()
    return df.sort_values("ts_ns").reset_index(drop=True)

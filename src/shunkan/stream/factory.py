"""Build the right tick feed for a watchlist: Kite WebSocket when a broker
is connected, synthetic random-walk otherwise. Shared by the TUI tape panel
and the web server's tick bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


def _no_resolver(symbol: str):
    return None


@dataclass
class Feed:
    ticker: object               # KiteTicker | SyntheticTicker
    tokens: list[int]
    names: dict[int, str]        # token -> display symbol
    live: bool                   # True = real exchange feed
    # symbol -> (token, display name), or None when the feed cannot stream it.
    # The tick bus uses this to honour runtime subscriptions; a feed without a
    # resolver simply reports every extra symbol as unknown, honestly.
    resolve: Callable[[str], tuple[int, str] | None] = field(default=_no_resolver)


def front_future_rows(nfo, names, today):
    """Nearest non-expired future per index name, from the NFO dump.

    Pure and testable: (token, display) pairs like (..., "NIFTYFUT"). The
    index itself prints no volume tick by tick; its front future does, and
    that tape is what VWAP and day-structure reads need."""
    import pandas as pd

    out = []
    for name in names:
        futs = nfo[(nfo["name"] == name) & (nfo["instrument_type"] == "FUT")].copy()
        if futs.empty:
            continue
        futs["expiry"] = pd.to_datetime(futs["expiry"])
        futs = futs[futs["expiry"].dt.date >= today]
        if futs.empty:
            continue
        front = futs.sort_values("expiry").iloc[0]
        out.append((int(front["instrument_token"]), f"{name}FUT"))
    return out


def ensure_front_futures(feed) -> list[str]:
    """Idempotent repair: make sure the live feed streams the front index
    futures, subscribing them onto the RUNNING ticker if the first build
    missed them (measured 2026-08-18: a startup race left a feed without
    its futures for the whole session, silently - the feed must not marry
    its first build). Returns the labels present after the attempt."""
    if not getattr(feed, "live", False):
        return []
    have = [n for n in feed.names.values() if n.endswith("FUT")]
    want = [n for n in ("NIFTY", "BANKNIFTY") if f"{n}FUT" not in feed.names.values()]
    if not want:
        return have
    from datetime import date

    from shunkan.data.brokers import get_broker
    from shunkan.data.kite_fno import load_instruments

    nfo = load_instruments(get_broker(), "NFO")
    fresh = []
    for token, label in front_future_rows(nfo, tuple(want), date.today()):
        if token not in feed.names:
            feed.names[token] = label
            feed.tokens.append(token)
            fresh.append((token, label))
    if fresh:
        feed.ticker.subscribe([t for t, _ in fresh])
    return have + [l for _, l in fresh]


def build_feed(watchlist: list[str]) -> Feed:
    from shunkan.data.brokers import KiteProvider, get_broker, load_credentials
    from shunkan.data.provider import DataError, is_offline
    from shunkan.stream import KiteTicker, SyntheticTicker

    if not is_offline():
        broker = None
        try:
            broker = get_broker()
        except DataError:
            pass
        if isinstance(broker, KiteProvider):
            try:
                from shunkan.data.kite_fno import load_instruments

                nse = load_instruments(broker, "NSE")
                tokens: list[int] = []
                names: dict[int, str] = {}
                # Indices stream too — well-known tokens from the dump.
                index_rows = nse[nse["segment"] == "INDICES"]
                for label, idx_name in (("NIFTY", "NIFTY 50"), ("BANKNIFTY", "NIFTY BANK")):
                    row = index_rows[index_rows["tradingsymbol"] == idx_name]
                    if not row.empty:
                        token = int(row["instrument_token"].iloc[0])
                        tokens.append(token)
                        names[token] = label
                for sym in watchlist:
                    name = sym.upper().removesuffix(".NS")
                    if name in names.values():
                        continue
                    match = nse[nse["tradingsymbol"] == name]
                    if not match.empty:
                        token = int(match["instrument_token"].iloc[0])
                        tokens.append(token)
                        names[token] = name
                # Front index futures ride along: their tape carries the
                # volume the index never prints, which is what makes VWAP
                # and any day-structure read possible at all.
                try:
                    from datetime import date as _date

                    nfo = load_instruments(broker, "NFO")
                    for token, label in front_future_rows(
                            nfo, ("NIFTY", "BANKNIFTY"), _date.today()):
                        if token not in names:
                            tokens.append(token)
                            names[token] = label
                except Exception as exc:
                    # The cash feed stands alone, but a missing futures tape
                    # must never be invisible: it silently costs the VWAP
                    # read its data. (Bare pass here hid a cold-cache failure
                    # on first container start; measured 2026-08-18.)
                    import logging

                    logging.getLogger("shunkan.feed").warning(
                        "front-future feed skipped: %s", exc)
                if tokens:
                    creds = load_credentials()["zerodha"]

                    def resolve_live(symbol: str, _nse=nse):
                        """NSE cash symbols and the two index labels. A miss
                        is a miss; the bus reports it rather than guessing."""
                        name = symbol.upper().removesuffix(".NS")
                        for label, idx in (("NIFTY", "NIFTY 50"),
                                           ("BANKNIFTY", "NIFTY BANK")):
                            if name == label:
                                row = _nse[_nse["tradingsymbol"] == idx]
                                if not row.empty:
                                    return int(row["instrument_token"].iloc[0]), label
                        m = _nse[(_nse["tradingsymbol"] == name)
                                 & (_nse["segment"] != "INDICES")]
                        if m.empty:
                            return None
                        return int(m["instrument_token"].iloc[0]), name

                    return Feed(
                        ticker=KiteTicker(creds["api_key"], creds["access_token"]),
                        tokens=tokens,
                        names=names,
                        live=True,
                        resolve=resolve_live,
                    )
            except Exception:
                pass  # instruments fetch failed — demo feed below

    ticker = SyntheticTicker(watchlist)

    def resolve_demo(symbol: str):
        # Demo can walk anything: register on demand so routing behaves the
        # same offline as live.
        name = symbol.upper().removesuffix(".NS")
        return ticker.add_symbol(name), name

    return Feed(
        ticker=ticker,
        tokens=list(ticker.tokens),
        names=dict(ticker.tokens),
        live=False,
        resolve=resolve_demo,
    )

"""Build the right tick feed for a watchlist: Kite WebSocket when a broker
is connected, synthetic random-walk otherwise. Shared by the TUI tape panel
and the web server's tick bridge."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Feed:
    ticker: object               # KiteTicker | SyntheticTicker
    tokens: list[int]
    names: dict[int, str]        # token -> display symbol
    live: bool                   # True = real exchange feed


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
                if tokens:
                    creds = load_credentials()["zerodha"]
                    return Feed(
                        ticker=KiteTicker(creds["api_key"], creds["access_token"]),
                        tokens=tokens,
                        names=names,
                        live=True,
                    )
            except Exception:
                pass  # instruments fetch failed — demo feed below

    ticker = SyntheticTicker(watchlist)
    return Feed(
        ticker=ticker,
        tokens=list(ticker.tokens),
        names=dict(ticker.tokens),
        live=False,
    )

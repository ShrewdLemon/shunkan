"""Option-chain resolution — real sources only, or an honest refusal.

Order: broker (Zerodha Kite) → NSE public API → nothing. There is no
synthetic fallback on the live path: when no source answers, get_chain
raises ChainUnavailable carrying the trail of what was tried. A simulated
chain exists only in offline/demo mode (SHUNKAN_OFFLINE=1), and it is
marked `is_model` so nothing downstream can mistake it for observation.
"""

from __future__ import annotations

from datetime import date

from shunkan.data.memcache import ttl_cache
from shunkan.data.provider import DataError, is_offline
from shunkan.derivatives.chain import OptionChain
from shunkan.derivatives.synthetic import synthetic_chain


def _capture(chain: OptionChain) -> OptionChain:
    """Persist real chains to the local store (synthetic/model are refused
    by the store itself). Capture rides the 60s TTL cache, so at most one
    snapshot per minute per symbol."""
    try:
        from shunkan.store import ChainStore

        ChainStore().snapshot(chain)
    except Exception:
        pass  # capture must never break the live path
    return chain


class ChainUnavailable(DataError):
    """No real option chain could be sourced.

    Subclasses DataError so every existing handler keeps catching it, and
    carries the trail so callers can show exactly which sources were tried
    and why each one failed.
    """

    def __init__(self, symbol: str, trail: list[str]) -> None:
        self.symbol = symbol
        self.source_trail = list(trail)
        super().__init__(
            f"No live option chain for {symbol} — "
            + ("; ".join(trail) if trail else "no source was reachable")
        )


@ttl_cache(ttl=60.0, max_items=32)
def _resolve(symbol: str, expiry: date | None = None) -> OptionChain | ChainUnavailable:
    """Resolve a chain, or the failure that explains why there isn't one.

    The failure is returned rather than raised so it rides the same TTL
    cache: a blocked NSE gets re-probed once a minute, not once per panel.
    """
    sym = symbol.upper()
    trail: list[str] = []

    # Offline/demo is the one place a simulated book is legitimate. It is
    # marked is_model, so the store refuses it and every derived number that
    # would otherwise inherit a real label is withheld instead.
    if is_offline():
        chain = synthetic_chain(sym, expiry=expiry)
        chain.source_trail = ["offline mode (SHUNKAN_OFFLINE=1)"]
        return chain

    # 1. Broker (exchange-grade, real-time) when Zerodha is connected.
    try:
        from shunkan.data.brokers import KiteProvider, get_broker
        from shunkan.data.kite_fno import kite_option_chain

        broker = get_broker()
        if isinstance(broker, KiteProvider):
            return _capture(kite_option_chain(broker, sym, expiry))
        trail.append("Kite: no broker connected")
    except DataError as exc:
        # A silent fallback is a lie of omission — name the reason.
        if "403" in str(exc):
            trail.append("Kite: 403 on /quote — daily access token expired; "
                         "run `shunkan connect zerodha` (Kite invalidates "
                         "tokens each morning)")
        else:
            trail.append(f"Kite: {str(exc)[:120]}")

    # 2. NSE public API (free, ~1 min delayed, bot-blocked on some networks).
    try:
        from shunkan.data.kite_fno import cached_lot_size
        from shunkan.data.nse import fetch_nse_chain

        chain = _capture(fetch_nse_chain(sym, expiry))
        # NSE's chain payload carries no contract lot; the NFO instruments
        # dump does, and it needs no broker. Without it we show a dash
        # rather than multiply every rupee figure by a guess.
        if chain.lot_size is None:
            chain.lot_size, chain.lot_size_source = cached_lot_size(sym, chain.expiry)
        chain.source_trail = trail
        return chain
    except DataError as exc:
        trail.append(f"NSE: {str(exc)[:120]}")

    # 3. There is no step 3. A simulated book here would put fabricated
    #    positioning behind PCR, max pain, the OI walls, ΔOI and every vote
    #    that reads them — wearing exactly the same UI as the real thing.
    return ChainUnavailable(sym, trail)


def get_chain(symbol: str, expiry: date | None = None) -> OptionChain:
    """The real chain, or a raise that names every source that failed."""
    result = _resolve(symbol, expiry)
    if isinstance(result, ChainUnavailable):
        # Raise a fresh instance — re-raising the cached one would grow its
        # traceback on every call for the life of the cache entry.
        raise ChainUnavailable(result.symbol, result.source_trail)
    return result

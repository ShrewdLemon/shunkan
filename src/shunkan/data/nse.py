"""NSE public-API client (option chains, index snapshots).

NSE's JSON endpoints are free but fronted by aggressive anti-bot checks: a
burst gets the IP blocked for days. Every request goes through `nse_www`, the
one polite client for the host, so chains share its cross-process gap, cookie
warm-up and breaker with every other fetch. Every call degrades gracefully —
callers get a clear DataError naming the reason. On the live path that is
where it stops: the resolver reports the failure rather than substituting a
modelled book.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np

from shunkan.data import nse_www
from shunkan.data.memcache import ttl_cache
from shunkan.data.provider import DataError
from shunkan.derivatives.chain import OptionChain
from shunkan.markets import (
    IST,
    FNO_INDICES,
    is_expired,
    time_to_expiry_years,
    today_ist,
)


def _get_json(path: str, params: dict | None = None) -> dict:
    try:
        data = nse_www.get_json(path, params)
    except DataError as exc:
        raise DataError(f"NSE API blocked or unavailable: {exc}") from exc
    if not isinstance(data, dict):
        raise DataError(f"NSE {path}: JSON {type(data).__name__}, not an object")
    return data


@ttl_cache(ttl=1800.0)
def _expiry_ladder(symbol: str) -> tuple[str, ...]:
    """NSE's listed expiries; the v3 chain endpoint serves one expiry per call."""
    info = _get_json("/api/option-chain-contract-info", {"symbol": symbol})
    return tuple(info.get("expiryDates") or ())


class NSEClient:
    def option_chain(self, symbol: str, expiry: date | None = None) -> OptionChain:
        sym = symbol.upper()
        ladder = list(_expiry_ladder(sym))
        live = _live(ladder)
        if not live:
            raise DataError(f"NSE listed no unexpired expiry for {sym}")
        target = expiry.strftime("%d-%b-%Y") if expiry is not None else None
        pick = target if target in live else live[0]
        kind = "Indices" if sym in FNO_INDICES else "Equity"
        payload = _get_json("/api/option-chain-v3", {"type": kind, "symbol": sym, "expiry": pick})
        records = {**(payload.get("records") or {}), "expiryDates": ladder}
        return _parse_chain(sym, {**payload, "records": records},
                            datetime.strptime(pick, "%d-%b-%Y").date())


def _live(expiries) -> list[str]:
    """NSE keeps the just-settled series listed after the 15:30 bell; trading it is impossible."""
    return [e for e in expiries if not is_expired(datetime.strptime(e, "%d-%b-%Y").date())]


def fetch_nse_chain(symbol: str, expiry: date | None = None) -> OptionChain:
    return NSEClient().option_chain(symbol, expiry)


def _parse_chain(symbol: str, payload: dict, want_expiry: date | None) -> OptionChain:
    records = payload.get("records") or {}
    rows = records.get("data") or []
    expiries = records.get("expiryDates") or []
    spot = float(records.get("underlyingValue") or 0.0)
    # NSE stamps the snapshot itself, e.g. "13-Aug-2026 15:29:00". It is
    # typically a minute or so behind, which is exactly why it must be shown
    # rather than replaced with the browser's clock.
    as_of = None
    raw_ts = records.get("timestamp") or ""
    for fmt_ in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            as_of = datetime.strptime(raw_ts, fmt_).replace(tzinfo=IST)
            break
        except (ValueError, TypeError):
            continue
    if not rows or not expiries or spot <= 0:
        raise DataError(f"NSE returned an empty chain for {symbol}")

    live = _live(expiries)
    if not live:
        raise DataError(f"NSE listed no unexpired expiry for {symbol}")

    if want_expiry is None:
        expiry_str = live[0]
    else:
        target = want_expiry.strftime("%d-%b-%Y")
        expiry_str = target if target in live else live[0]
    expiry = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    t_years = time_to_expiry_years(expiry)

    strikes, fields = [], {k: [] for k in (
        "c_ltp", "c_oi", "c_chg", "c_vol", "c_iv", "p_ltp", "p_oi", "p_chg", "p_vol", "p_iv"
    )}
    for row in rows:
        if (row.get("expiryDate") or row.get("expiryDates")) != expiry_str:
            continue
        strikes.append(float(row["strikePrice"]))
        ce, pe = row.get("CE") or {}, row.get("PE") or {}
        fields["c_ltp"].append(float(ce.get("lastPrice") or 0.0))
        fields["c_oi"].append(float(ce.get("openInterest") or 0.0))
        fields["c_chg"].append(float(ce.get("changeinOpenInterest") or 0.0))
        fields["c_vol"].append(float(ce.get("totalTradedVolume") or 0.0))
        iv_c = float(ce.get("impliedVolatility") or 0.0)
        fields["c_iv"].append(iv_c / 100.0 if iv_c > 0 else np.nan)
        fields["p_ltp"].append(float(pe.get("lastPrice") or 0.0))
        fields["p_oi"].append(float(pe.get("openInterest") or 0.0))
        fields["p_chg"].append(float(pe.get("changeinOpenInterest") or 0.0))
        fields["p_vol"].append(float(pe.get("totalTradedVolume") or 0.0))
        iv_p = float(pe.get("impliedVolatility") or 0.0)
        fields["p_iv"].append(iv_p / 100.0 if iv_p > 0 else np.nan)

    if not strikes:
        raise DataError(f"No strikes for {symbol} expiry {expiry_str}")

    order = np.argsort(strikes)
    arr = lambda key: np.asarray(fields[key], dtype=np.float64)[order]  # noqa: E731
    return OptionChain(
        symbol=symbol,
        spot=spot,
        expiry=expiry,
        t_years=t_years,
        strikes=np.asarray(strikes, dtype=np.float64)[order],
        call_ltp=arr("c_ltp"),
        call_oi=arr("c_oi"),
        call_oi_change=arr("c_chg"),
        call_volume=arr("c_vol"),
        call_iv=arr("c_iv"),
        put_ltp=arr("p_ltp"),
        put_oi=arr("p_oi"),
        put_oi_change=arr("p_chg"),
        put_volume=arr("p_vol"),
        put_iv=arr("p_iv"),
        source="NSE (live, ~1min delayed)",
        is_model=False,
        as_of=as_of,
        # the listed ladder minus anything already settled, so the UI can
        # offer an expiry selector that only shows tradeable series
        expiries=[datetime.strptime(e, "%d-%b-%Y").date() for e in live],
    )

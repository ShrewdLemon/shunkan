from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from shunkan.markets import (
    session_phase,
    MARKET_CLOSE,
    IST,
    MIN_TTE_SECONDS,
    SECONDS_PER_YEAR,
    denormalize_symbol,
    is_expired,
    normalize_symbol,
    time_to_expiry_years,
)


def test_bare_symbols_get_ns_suffix():
    assert normalize_symbol("RELIANCE") == "RELIANCE.NS"
    assert normalize_symbol("hdfcbank") == "HDFCBANK.NS"


def test_index_aliases():
    assert normalize_symbol("NIFTY") == "^NSEI"
    assert normalize_symbol("banknifty") == "^NSEBANK"
    assert normalize_symbol("SENSEX") == "^BSESN"
    assert normalize_symbol("VIX") == "^INDIAVIX"
    assert normalize_symbol("USDINR") == "INR=X"


def test_explicit_tickers_pass_through():
    assert normalize_symbol("^GSPC") == "^GSPC"
    assert normalize_symbol("RELIANCE.NS") == "RELIANCE.NS"
    assert normalize_symbol("BTC-USD") == "BTC-USD"
    assert normalize_symbol("GC=F") == "GC=F"
    assert normalize_symbol("AAPL.MX") == "AAPL.MX"


def test_denormalize_round_trip():
    assert denormalize_symbol("^NSEI") == "NIFTY"
    assert denormalize_symbol("RELIANCE.NS") == "RELIANCE"
    assert denormalize_symbol("^GSPC") == "^GSPC"


# -- expiry clock -------------------------------------------------------------


def test_time_to_expiry_decays_through_the_expiry_session():
    # CAS era (2026-08-03): derivatives trade 09:15-15:40, so the expiry-day
    # session is 6h25m. Derived from MARKET_CLOSE, not hardcoded - these
    # tests fossilised the pre-CAS 15:30 bell once already.
    expiry = date(2026, 6, 18)  # Thursday
    session_s = (MARKET_CLOSE.hour * 3600 + MARKET_CLOSE.minute * 60) - (9 * 3600 + 15 * 60)
    at_open = time_to_expiry_years(expiry, datetime(2026, 6, 18, 9, 15, tzinfo=IST))
    at_three = time_to_expiry_years(expiry, datetime(2026, 6, 18, 15, 0, tzinfo=IST))
    assert at_open == pytest.approx(session_s / SECONDS_PER_YEAR)
    assert at_three == pytest.approx(
        ((MARKET_CLOSE.hour - 15) * 3600 + MARKET_CLOSE.minute * 60) / SECONDS_PER_YEAR)
    assert at_three < at_open / 9


def test_time_to_expiry_floors_at_the_bell_instead_of_zero():
    expiry = date(2026, 6, 18)
    bell = time_to_expiry_years(expiry, datetime(
        2026, 6, 18, MARKET_CLOSE.hour, MARKET_CLOSE.minute, tzinfo=IST))
    assert bell == pytest.approx(MIN_TTE_SECONDS / SECONDS_PER_YEAR)
    assert bell > 0  # Black-Scholes must never divide by zero time


def test_time_to_expiry_is_the_same_from_any_host_timezone():
    when = datetime(2026, 6, 17, 22, 0, tzinfo=IST)  # 18 Jun in IST, 17 Jun in NY
    expiry = date(2026, 6, 18)
    assert time_to_expiry_years(expiry, when) == time_to_expiry_years(
        expiry, when.astimezone(ZoneInfo("America/New_York"))
    )


def test_contract_is_live_until_its_close_bell():
    expiry = date(2026, 6, 18)
    assert not is_expired(expiry, datetime(2026, 6, 18, 15, 39, tzinfo=IST))
    assert is_expired(expiry, datetime(2026, 6, 18, 15, 41, tzinfo=IST))


def test_every_session_phase_has_a_news_timing_factor():
    """A phase the impact model does not know must degrade, not raise. The
    CAS rollout added two phases and the news panel died with a KeyError
    for the twenty minutes a day they were active."""
    from datetime import datetime, timedelta

    from shunkan.intel.impact import timing_factor

    seen = set()
    t = datetime(2026, 8, 24, 0, 0, tzinfo=IST)
    for _ in range(24 * 12):        # every five minutes of a weekday
        seen.add(session_phase(t).phase)
        t += timedelta(minutes=5)
    for phase in seen:
        mult, horizon = timing_factor(phase)
        assert mult > 0 and horizon
    assert {"auction", "closing_deriv"} <= seen      # CAS is modelled
    assert timing_factor("a_phase_invented_in_2030")[0] == 1.0

"""The option-chain client: NSE's v3 endpoint, through the one polite client for www.nseindia.com."""

import inspect
from datetime import date

import pytest

from shunkan.data import nse, nse_www
from shunkan.data.provider import DataError, FetchFailed

LADDER = ["01-Jan-2020", "18-Aug-2099", "25-Aug-2099"]


def v3(expiry, spot=24500.0):
    return {"records": {
        "timestamp": "13-Aug-2026 15:29:00", "underlyingValue": spot, "expiryDates": [expiry],
        "data": [{"expiryDates": expiry, "strikePrice": k,
                  "CE": {"lastPrice": 100.0, "openInterest": 1000, "changeinOpenInterest": 10,
                         "totalTradedVolume": 500, "impliedVolatility": 12.5},
                  "PE": {"lastPrice": 90.0, "openInterest": 900, "changeinOpenInterest": -5,
                         "totalTradedVolume": 400, "impliedVolatility": 13.0}}
                 for k in (24600, 24500)],
    }}


@pytest.fixture(autouse=True)
def fresh_ladder():
    nse._expiry_ladder.cache.clear()
    yield
    nse._expiry_ladder.cache.clear()


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake(path, params=None):
        log.append((path, params))
        if path == "/api/option-chain-contract-info":
            return {"expiryDates": LADDER, "strikePrice": []}
        return v3(params["expiry"])

    monkeypatch.setattr(nse_www, "get_json", fake)
    return log


def test_the_ladder_then_the_first_live_expiry(calls):
    c = nse.NSEClient().option_chain("nifty")
    assert calls == [("/api/option-chain-contract-info", {"symbol": "NIFTY"}),
                     ("/api/option-chain-v3", {"type": "Indices", "symbol": "NIFTY", "expiry": "18-Aug-2099"})]
    assert c.expiry == date(2099, 8, 18) and c.spot == 24500.0
    assert list(c.strikes) == [24500.0, 24600.0]
    assert c.expiries == [date(2099, 8, 18), date(2099, 8, 25)]


def test_a_listed_expiry_is_asked_for_and_the_ladder_is_cached(calls):
    nse.NSEClient().option_chain("NIFTY")
    c = nse.NSEClient().option_chain("NIFTY", date(2099, 8, 25))
    assert c.expiry == date(2099, 8, 25)
    assert [p for p, _ in calls].count("/api/option-chain-contract-info") == 1
    assert calls[-1][1]["expiry"] == "25-Aug-2099"


def test_an_unlisted_expiry_falls_back_to_the_first_live(calls):
    assert nse.NSEClient().option_chain("NIFTY", date(2099, 9, 1)).expiry == date(2099, 8, 18)


def test_stock_chains_ask_for_the_equity_type(calls):
    nse.NSEClient().option_chain("RELIANCE")
    assert calls[-1] == ("/api/option-chain-v3", {"type": "Equity", "symbol": "RELIANCE", "expiry": "18-Aug-2099"})


def test_a_block_is_a_data_error_naming_the_reason(monkeypatch):
    def blocked(path, params=None):
        raise FetchFailed("breaker open until 02:30 IST")

    monkeypatch.setattr(nse_www, "get_json", blocked)
    with pytest.raises(DataError, match="breaker open until 02:30 IST"):
        nse.NSEClient().option_chain("NIFTY")


def test_a_non_object_answer_is_a_data_error(monkeypatch):
    monkeypatch.setattr(nse_www, "get_json", lambda path, params=None: [])
    with pytest.raises(DataError, match="not an object"):
        nse.NSEClient().option_chain("NIFTY")


def test_no_live_expiry_is_a_data_error(monkeypatch):
    monkeypatch.setattr(nse_www, "get_json", lambda path, params=None: {"expiryDates": ["01-Jan-2020"]})
    with pytest.raises(DataError, match="no unexpired expiry"):
        nse.NSEClient().option_chain("NIFTY")


def test_the_module_opens_no_http_client_of_its_own():
    assert "httpx" not in inspect.getsource(nse)

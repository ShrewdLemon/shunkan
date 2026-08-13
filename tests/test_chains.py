"""Chain resolution: what happens when no real source answers.

The contract these tests pin down is the product's hard rule — on the live
path Shunkan reports that it could not source a chain, and never substitutes
a modelled one that would render as market data.
"""

from __future__ import annotations

import pytest

from shunkan.data import chains
from shunkan.data.chains import ChainUnavailable, get_chain
from shunkan.data.provider import DataError
from shunkan.store import ChainStore


@pytest.fixture
def online(monkeypatch):
    """Run as if a network were available. The TTL cache is cleared on both
    sides so a cached refusal cannot leak between tests."""
    monkeypatch.delenv("SHUNKAN_OFFLINE", raising=False)
    chains._resolve.cache.clear()
    yield
    chains._resolve.cache.clear()


def test_online_failure_raises_chain_unavailable_with_trail(online, monkeypatch):
    monkeypatch.setattr("shunkan.data.brokers.get_broker", lambda: None)

    def blocked(*a, **k):
        raise DataError("blocked")

    monkeypatch.setattr("shunkan.data.nse.fetch_nse_chain", blocked)

    with pytest.raises(ChainUnavailable) as exc_info:
        get_chain("NIFTY")

    exc = exc_info.value
    assert isinstance(exc, DataError)  # every existing handler still catches it
    assert any(t.startswith("Kite:") for t in exc.source_trail)
    assert any(t.startswith("NSE:") for t in exc.source_trail)
    for entry in exc.source_trail:
        assert entry in str(exc)


def test_offline_returns_marked_model_chain(tmp_path):
    """Offline is the one place a modelled chain is legitimate — and even
    there it stays marked and stays out of the store."""
    chains._resolve.cache.clear()
    c = get_chain("NIFTY")
    assert c.is_model is True
    assert c.source_trail == ["offline mode (SHUNKAN_OFFLINE=1)"]

    ChainStore(root=tmp_path).snapshot(c)
    assert ChainStore(root=tmp_path).snapshots_today("NIFTY") is None


def test_failure_is_cached_for_the_ttl(online, monkeypatch):
    """Without the _resolve/get_chain split every panel re-probes a blocked
    source, which is how one dead network became a request storm."""
    calls = []

    def blocked(*a, **k):
        calls.append(1)
        raise DataError("blocked")

    monkeypatch.setattr("shunkan.data.brokers.get_broker", lambda: None)
    monkeypatch.setattr("shunkan.data.nse.fetch_nse_chain", blocked)

    for _ in range(3):
        with pytest.raises(ChainUnavailable):
            get_chain("NIFTY")

    assert len(calls) == 1

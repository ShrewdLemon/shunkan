"""Guards that only matter when Shunkan is reachable by someone else.

All of them are inert on a default localhost run, which is why they need
tests: nothing in normal use would ever notice them regressing.
"""

from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from shunkan.server import create_app

TOKEN = "test-token-value"


@pytest.fixture
def guarded():
    """An app built the way `shunkan serve` builds it for a non-loopback bind.

    "testserver" is in the allow-list because TestClient hardcodes it as the
    Host on websocket handshakes regardless of base_url. Host refusal gets its
    own test rather than being tangled into every other one.
    """
    app = create_app(access_token=TOKEN,
                     allowed_hosts=("myhost", "127.0.0.1", "testserver"))
    with TestClient(app, base_url="http://myhost") as c:
        yield c


@pytest.fixture
def plain():
    with TestClient(create_app()) as c:
        yield c


def test_default_app_is_wide_open_on_purpose(plain):
    """A localhost run must not need a token. If this starts failing, the
    guards stopped being opt-in and every normal user just got locked out."""
    assert plain.get("/api/status").status_code == 200


def test_api_requires_the_token_when_one_is_set(guarded):
    assert guarded.get("/api/status").status_code == 401
    assert guarded.get("/api/status", headers={"X-Shunkan-Token": TOKEN}).status_code == 200
    assert guarded.get(f"/api/status?t={TOKEN}").status_code == 200


def test_a_wrong_token_is_refused(guarded):
    assert guarded.get("/api/status", headers={"X-Shunkan-Token": "nope"}).status_code == 401


def test_compare_survives_non_ascii():
    """hmac.compare_digest raises UnicodeEncodeError on a non-ASCII str, which
    would turn a junk token into an unhandled 500 instead of a clean refusal.
    Tested directly because httpx will not put a non-ASCII byte in a header."""
    from shunkan.server.api import _same_secret

    assert _same_secret("ü", TOKEN) is False
    assert _same_secret(TOKEN, TOKEN) is True


def test_unknown_host_is_refused():
    """Rebinding defence: a page that has DNS-rebound to this port is
    same-origin, so only the Host allow-list stops it."""
    app = create_app(access_token=TOKEN, allowed_hosts=("myhost",))
    with TestClient(app, base_url="http://myhost") as c:
        assert c.get("/api/status", headers={"Host": "evil.example",
                                             "X-Shunkan-Token": TOKEN}).status_code == 403


def test_cross_origin_writes_are_refused(guarded):
    """A DNS-rebound page is same-origin, so CORS never fires and these
    bodyless POSTs are CORS-simple. The Origin check is the only thing here."""
    r = guarded.post("/api/portfolio/margin",
                     headers={"X-Shunkan-Token": TOKEN, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_the_tick_socket_is_gated_too(guarded):
    """Starlette's HTTP middleware never sees a websocket scope, so the route
    guards itself. This socket carries the licensed exchange tape, so an
    ungated one is a redistribution problem and not only a security one."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with guarded.websocket_connect("/ws/ticks"):
            pass
    with guarded.websocket_connect(f"/ws/ticks?t={TOKEN}") as ws:
        assert ws is not None


def test_credentials_are_written_owner_only(tmp_path, monkeypatch):
    import shunkan.data.brokers as brokers

    target = tmp_path / "credentials.json"
    monkeypatch.setattr(brokers, "CREDENTIALS_FILE", target)
    monkeypatch.setattr(brokers, "ensure_dirs", lambda: None, raising=False)
    brokers.save_credentials("zerodha", api_key="k", api_secret="s")

    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))  # no half-written file left behind


def test_no_broker_response_body_is_formatted_into_an_error():
    """Those bodies are the session payload on a 200, carrying public_token
    and refresh_token, and the error reaches a browser toast."""
    import pathlib
    src = pathlib.Path(brokers_path()).read_text()
    assert "resp.text[:200]" not in src


def brokers_path():
    import shunkan.data.brokers as brokers
    return brokers.__file__

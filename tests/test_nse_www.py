"""www.nseindia.com: one request at a time, 10 s apart, across processes, with a breaker."""

from __future__ import annotations

import fcntl
import os
from datetime import datetime

import httpx
import pytest

from shunkan.config import CACHE_DIR
from shunkan.data import nse_www
from shunkan.data.nse_www import BASE, BREAKER_S, MIN_GAP_S, Client
from shunkan.data.provider import FetchFailed, Unpublished
from shunkan.markets import IST

T0 = datetime(2026, 11, 4, 18, 0, tzinfo=IST).timestamp()
API = "/api/fiidiiTradeReact"
DENIED = "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission"


class Clock:
    def __init__(self):
        self.t = [T0]
        self.slept = []

    def __call__(self):
        return self.t[0]

    def sleep(self, s):
        self.slept.append(s)
        self.t[0] += s


class Server:
    def __init__(self, *api, home=200, on_request=None):
        self.api = list(api)
        self.home = home
        self.on_request = on_request
        self.seen = []

    def __call__(self, request):
        self.seen.append(request)
        if self.on_request:
            self.on_request(request)
        if request.url.path == "/":
            return httpx.Response(self.home, text="<html>home</html>",
                                  headers={"set-cookie": "nsit=abc; Path=/"})
        item = self.api.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, text=body)

    @property
    def paths(self):
        return [r.url.path for r in self.seen]


@pytest.fixture
def clock():
    return Clock()


def make(state_dir, server, clock):
    return Client(state_dir=state_dir, clock=clock, sleep=clock.sleep,
                  transport=httpx.MockTransport(server))


def _locked_elsewhere(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_constants():
    assert BASE == "https://www.nseindia.com"
    assert MIN_GAP_S == 10.0 and BREAKER_S == 1800.0


def test_default_state_dir_is_under_the_cache():
    assert Client().state_dir == CACHE_DIR / "nse_www"


def test_fresh_state_dir_is_owner_only(tmp_path, clock):
    state = tmp_path / "a" / "nse_www"
    make(state, Server((200, "[]")), clock).get_json(API)
    assert state.stat().st_mode & 0o777 == 0o700


def test_first_call_warms_up_then_waits_the_gap_before_the_api(tmp_path, clock):
    server = Server((200, '{"a": 1}'))
    assert make(tmp_path, server, clock).get_json(API) == {"a": 1}
    assert server.paths == ["/", API]
    assert clock.slept == [MIN_GAP_S]
    assert float((tmp_path / "last").read_text()) == T0 + MIN_GAP_S


def test_api_request_is_json_with_referer_browser_ua_and_warmup_cookie(tmp_path, clock):
    server = Server((200, "[]"))
    assert make(tmp_path, server, clock).get_json("api/holiday-master", {"type": "trading"}) == []
    home, api = server.seen
    assert str(home.url) == BASE + "/"
    assert api.url.path == "/api/holiday-master" and api.url.params["type"] == "trading"
    assert api.headers["accept"] == "application/json"
    assert api.headers["referer"] == BASE + "/"
    assert "Chrome" in api.headers["user-agent"] and "Mozilla" in api.headers["user-agent"]
    assert "nsit=abc" in api.headers["cookie"]
    allowed = {"host", "accept", "accept-encoding", "connection", "user-agent", "referer", "cookie"}
    assert set(api.headers.keys()) <= allowed
    assert set(home.headers.keys()) <= allowed


def test_warmup_status_is_ignored(tmp_path, clock):
    server = Server((200, '{"ok": true}'), home=403)
    assert make(tmp_path, server, clock).get_json(API) == {"ok": True}
    assert server.paths == ["/", API]


def test_warmup_happens_once_per_client(tmp_path, clock):
    server = Server((200, "[1]"), (200, "[2]"))
    c = make(tmp_path, server, clock)
    assert c.get_json(API) == [1] and c.get_json(API) == [2]
    assert server.paths == ["/", API, API]
    assert clock.slept == [MIN_GAP_S, MIN_GAP_S]


def test_second_client_in_same_state_dir_waits_for_the_gap_left_by_the_first(tmp_path, clock):
    make(tmp_path, Server((200, "[]")), clock).get_json(API)
    clock.t[0] += 4
    clock.slept.clear()
    make(tmp_path, Server((200, "[]")), clock).get_json(API)
    assert clock.slept == pytest.approx([6.0, MIN_GAP_S])


def test_a_last_stamp_from_the_future_never_waits_more_than_the_gap(tmp_path, clock):
    (tmp_path / "last").write_text(repr(T0 + 3600))
    make(tmp_path, Server((200, "[]")), clock).get_json(API)
    assert clock.slept == [MIN_GAP_S, MIN_GAP_S]


def test_lock_is_held_across_every_wait_and_request(tmp_path, clock):
    lock = tmp_path / "lock"
    held = []
    sleep = clock.sleep

    def watched_sleep(s):
        held.append(("sleep", _locked_elsewhere(lock)))
        sleep(s)

    clock.sleep = watched_sleep
    server = Server((200, "[]"), on_request=lambda r: held.append((r.url.path, _locked_elsewhere(lock))))
    make(tmp_path, server, clock).get_json(API)
    assert held == [("/", True), ("sleep", True), (API, True)]
    assert not _locked_elsewhere(lock)


def test_html_body_opens_the_breaker_and_the_next_call_makes_no_request(tmp_path, clock):
    server = Server((200, DENIED))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match="^blocked: <HTML><HEAD><TITLE>Access Denied"):
        c.get_json(API)
    n = len(server.seen)
    with pytest.raises(FetchFailed, match="breaker open until 18:30 IST"):
        c.get_json(API)
    with pytest.raises(FetchFailed, match="breaker open"):
        make(tmp_path, server, clock).get_json(API)
    assert len(server.seen) == n
    assert float((tmp_path / "breaker").read_text()) == T0 + MIN_GAP_S + BREAKER_S


@pytest.mark.parametrize("body", ["", "not json {", "  <!DOCTYPE html><html>"])
def test_unparseable_body_is_blocked_and_opens_the_breaker(tmp_path, clock, body):
    with pytest.raises(FetchFailed, match="^blocked"):
        make(tmp_path, Server((200, body)), clock).get_json(API)
    assert (tmp_path / "breaker").exists()


def test_breaker_expires_after_1800_s_of_fake_time(tmp_path, clock):
    server = Server((200, DENIED), (200, '{"ok": 1}'))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match="blocked"):
        c.get_json(API)
    n = len(server.seen)
    clock.t[0] += BREAKER_S - 1
    with pytest.raises(FetchFailed, match="breaker open"):
        c.get_json(API)
    assert len(server.seen) == n
    clock.t[0] += 1
    assert c.get_json(API) == {"ok": 1}
    assert server.paths[n:] == ["/", API]


def test_403_then_200_succeeds_with_one_rewarm(tmp_path, clock):
    server = Server((403, "denied"), (200, '{"ok": 1}'))
    assert make(tmp_path, server, clock).get_json(API) == {"ok": 1}
    assert server.paths == ["/", API, "/", API]
    assert clock.slept == [MIN_GAP_S] * 3
    assert not (tmp_path / "breaker").exists()


@pytest.mark.parametrize("first,second", [(403, 403), (401, 403), (403, 401)])
def test_401_403_after_the_rewarm_opens_the_breaker(tmp_path, clock, first, second):
    server = Server((first, ""), (second, ""))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match=f"^blocked: HTTP {second}"):
        c.get_json(API)
    assert server.paths == ["/", API, "/", API]
    with pytest.raises(FetchFailed, match="breaker open"):
        c.get_json(API)
    assert len(server.seen) == 4


@pytest.mark.parametrize("body", ["Not Found", "<html>Not Found</html>"])
def test_404_is_unpublished_without_breaker(tmp_path, clock, body):
    server = Server((404, body), (200, "[]"))
    c = make(tmp_path, server, clock)
    with pytest.raises(Unpublished):
        c.get_json(API)
    assert c.get_json(API) == []
    assert server.paths == ["/", API, API]


@pytest.mark.parametrize("status,body", [(500, '{"error": "x"}'), (503, ""), (502, "oops")])
def test_5xx_without_html_is_failed_without_breaker(tmp_path, clock, status, body):
    server = Server((status, body), (200, "[]"))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match=f"HTTP {status}") as exc:
        c.get_json(API)
    assert not str(exc.value).startswith("blocked")
    assert c.get_json(API) == []


@pytest.mark.parametrize("body", ["", '{"msg": "slow down"}', "<html>Too Many</html>"])
def test_429_is_a_block_without_rewarm(tmp_path, clock, body):
    server = Server((429, body))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match="^blocked: HTTP 429"):
        c.get_json(API)
    assert server.paths == ["/", API]
    with pytest.raises(FetchFailed, match="breaker open"):
        c.get_json(API)
    assert len(server.seen) == 2


@pytest.mark.parametrize("status", [503, 500, 302])
def test_non_200_html_body_is_a_block(tmp_path, clock, status):
    server = Server((status, "  <HTML><HEAD><TITLE>Service Unavailable</TITLE>"))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match=f"^blocked: HTTP {status} <HTML><HEAD><TITLE>Service"):
        c.get_json(API)
    with pytest.raises(FetchFailed, match="breaker open"):
        c.get_json(API)
    assert server.paths == ["/", API]


def test_network_error_is_failed_without_breaker(tmp_path, clock):
    server = Server(httpx.ConnectError("refused"), (200, "[]"))
    c = make(tmp_path, server, clock)
    with pytest.raises(FetchFailed, match="ConnectError"):
        c.get_json(API)
    assert c.get_json(API) == []
    assert not (tmp_path / "breaker").exists()


@pytest.mark.parametrize("body", ['"text"', "null", "3"])
def test_json_that_is_not_an_object_or_array_is_failed_without_breaker(tmp_path, clock, body):
    with pytest.raises(FetchFailed, match="not an object or array"):
        make(tmp_path, Server((200, body)), clock).get_json(API)
    assert not (tmp_path / "breaker").exists()


def test_module_get_json_shares_one_client(monkeypatch):
    made = []

    class Fake:
        def __init__(self):
            made.append(self)

        def get_json(self, path, params=None):
            return [path, params]

    monkeypatch.setattr(nse_www, "Client", Fake)
    monkeypatch.setattr(nse_www, "_shared", None)
    assert nse_www.get_json("/api/a") == ["/api/a", None]
    assert nse_www.get_json("/api/b", {"x": 1}) == ["/api/b", {"x": 1}]
    assert len(made) == 1

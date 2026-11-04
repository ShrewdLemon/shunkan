import asyncio
import threading
import time
import types

from fastapi import APIRouter
from fastapi.testclient import TestClient

from shunkan import plugins
from shunkan.server.api import create_app


def _plugin(name="fake", fail=False, started=None):
    def register(host):
        r = APIRouter()

        @r.get("/api/fake/ping")
        def ping():
            return {"ok": True, "has_provider": "provider" in host.context}

        host.router(r)
        host.view("fake", "FAK", "Fake view", f"/plugins/{name}/fake.js", symbol=True)
        host.status("fake", lambda: {"alive": True})
        host.symbols(lambda q, limit: [{"symbol": "FAKECO", "name": "Fake Co", "source": "fake"}])
        if started is not None:
            async def loop():
                started.set()
                await asyncio.sleep(3600)
            host.loop("fake_loop", loop)
        if fail:
            raise RuntimeError("boom")

    return types.SimpleNamespace(__name__=name, register=register)


def test_no_plugins_lists_nothing():
    with TestClient(create_app(plugins=[])) as c:
        assert c.get("/api/plugins").json() == []
        assert c.get("/api/status").json()["plugins"] == {"loaded": [], "failed": {}, "loops": {}}


def test_plugin_registers_router_view_status_and_symbols():
    with TestClient(create_app(plugins=[_plugin()])) as c:
        assert c.get("/api/fake/ping").json() == {"ok": True, "has_provider": True}
        views = c.get("/api/plugins").json()
        assert views == [{"id": "fake", "code": "FAK", "label": "Fake view",
                          "module": "/plugins/fake/fake.js", "symbol": True,
                          "group": "CONTEXT", "blurb": ""}]
        st = c.get("/api/status").json()
        assert st["plugins"] == {"loaded": ["fake"], "failed": {}, "loops": {}}
        assert st["fake"] == {"alive": True}


def test_failing_plugin_is_reported_and_leaves_nothing_behind():
    with TestClient(create_app(plugins=[_plugin(fail=True)])) as c:
        assert c.get("/api/plugins").json() == []
        assert c.get("/api/fake/ping").status_code == 404
        failed = c.get("/api/status").json()["plugins"]["failed"]
        assert failed == {"fake": "RuntimeError: boom"}


def test_plugin_loop_runs_in_lifespan():
    started = threading.Event()
    with TestClient(create_app(plugins=[_plugin(started=started)])) as c:
        assert started.wait(2.0)
        loop = c.get("/api/status").json()["plugins"]["loops"]["fake_loop"]
        assert loop["running"] is True and loop["failed"] == 0


def test_crashing_plugin_loop_is_reported_and_shutdown_is_clean():
    def register(host):
        async def loop():
            raise RuntimeError("loop boom")
        host.loop("bad_loop", loop)

    bad = types.SimpleNamespace(__name__="bad", register=register)
    with TestClient(create_app(plugins=[bad])) as c:
        deadline = time.monotonic() + 2.0
        while True:
            st = c.get("/api/status").json()["plugins"]
            loop = st["loops"]["bad_loop"]
            if loop["failed"] or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        assert st["loaded"] == ["bad"]
        assert loop["failed"] == 1 and loop["running"] is False
        assert loop["last_error"] == "RuntimeError: loop boom"


def test_missing_static_dir_fails_only_that_plugin(tmp_path):
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    (ok_dir / "ok.js").write_text("export const ok = 1;")

    def register_ok(host):
        host.static(ok_dir)

    def register_missing(host):
        host.static(tmp_path / "missing")

    ok = types.SimpleNamespace(__name__="okstatic", register=register_ok)
    missing = types.SimpleNamespace(__name__="nostatic", register=register_missing)
    with TestClient(create_app(plugins=[missing, ok])) as c:
        st = c.get("/api/status").json()["plugins"]
        assert st["loaded"] == ["okstatic"]
        assert st["failed"]["nostatic"].startswith("FileNotFoundError: ")
        assert c.get("/plugins/okstatic/ok.js").text == "export const ok = 1;"
        assert c.get("/plugins/nostatic/x.js").status_code == 404


def test_load_discovers_entry_points(monkeypatch):
    ep = types.SimpleNamespace(name="ep", load=lambda: _plugin("ep"))
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep] if group == plugins.GROUP else [])
    host = plugins.load()
    assert host.loaded == ["ep"] and [v.id for v in host.views] == ["fake"]


def _ranker(rows):
    def register(host):
        host.symbols(lambda q, limit: rows)
    return types.SimpleNamespace(__name__="ranker", register=register)


def test_plugin_rank_breaks_ties_within_a_match_tier(monkeypatch):
    from shunkan.data import constituents

    uni = [constituents.Constituent("AAA", "Alpha A Ltd", ("NIFTY500",), "X"),
           constituents.Constituent("AAB", "Alpha B Ltd", ("NIFTY500",), "X"),
           constituents.Constituent("AA", "Aa Exact Ltd", ("NIFTY500",), "X")]
    monkeypatch.setattr(constituents, "universe", lambda indices=(): uni)

    def search(app):
        return TestClient(app).get("/api/symbols/search?q=aa").json()["matches"]

    assert [m["symbol"] for m in search(create_app(plugins=[]))] == ["AA", "AAA", "AAB"]
    rows = search(create_app(plugins=[_ranker([
        {"symbol": "AAA", "rank": -1.0}, {"symbol": "AAB", "rank": -9.0},
        {"symbol": "AA", "rank": 5.0}, {"symbol": "NEWCO", "name": "New Co", "rank": -99.0}])]))
    assert [m["symbol"] for m in rows] == ["AA", "AAB", "AAA", "NEWCO"]
    assert all("rank" not in m for m in rows)
    assert rows[1] == {"symbol": "AAB", "name": "Alpha B Ltd", "industry": "X"}

"""A corrupt graph must announce itself.

On 2026-08-30 ~/.shunkan/shunkan.db was truncated from roughly 187 MB to
31.9 MB while a server process held it open across a laptop sleep. Every
b-tree kept pointers to pages past the new end of file. The damage was
invisible in the obvious place: `SELECT COUNT(*) FROM node` still returned a
number - 4,374, where there had been 62,220 - and only a query that happened
to touch a lost page raised "database disk image is malformed".

A graph reporting a plausible count while missing 90% of itself is worse than
one that refuses, so health() is checked here against a file damaged the same
way rather than against a mock.
"""
from __future__ import annotations

import sqlite3

import pytest

from shunkan.store.graph import GraphStore, check_health


def _truncated_db(tmp_path):
    """Build a real database, then cut its tail off.

    Truncation is the failure mode actually observed, and it is the one a
    mocked "return not ok" test would never have caught: the file stays a
    valid SQLite header with a valid schema and readable early pages.
    """
    path = tmp_path / "g.db"
    g = GraphStore(path)
    for i in range(4000):
        nid = g.put_node("company", f"SYM{i}", f"Company Number {i} Limited",
                         {"filler": "x" * 200})
        if i:
            g.put_edges([{"src": nid, "dst": f"company:SYM{i - 1}",
                          "rel": "related_party_of", "source": "test"}])
    g.commit()
    g._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    g._con.close()
    size = path.stat().st_size
    assert size > 200_000, "fixture too small to truncate meaningfully"
    with open(path, "r+b") as fh:
        fh.truncate(size // 3)
    return path


def test_health_passes_on_a_sound_database(tmp_path):
    g = GraphStore(tmp_path / "ok.db")
    g.put_node("company", "TEST", "Test Limited")
    g.commit()
    h = g.health()
    assert h["ok"] is True
    assert h["errors"] == []
    assert h["error_count"] == 0


def test_health_detects_truncation(tmp_path):
    h = check_health(_truncated_db(tmp_path))
    assert h["ok"] is False, "a truncated database reported itself sound"
    assert h["errors"], "damage found but no error text returned"
    assert "rebuild" in h["detail"], \
        "the verdict must tell the reader what to do about it"


def test_health_caps_its_error_report(tmp_path):
    """quick_check returns ONE row whose text is every complaint joined by
    newlines - 100 of them on the real failure. Slicing the row list caps
    nothing, and a status endpoint would return a wall of page numbers."""
    h = check_health(_truncated_db(tmp_path))
    assert len(h["errors"]) <= 15
    if h["error_count"] > 15:
        assert h["truncated_report"] is True
    for line in h["errors"]:
        assert "\n" not in line, "errors must be split into lines, not one blob"


def test_health_survives_a_file_that_cannot_be_opened(tmp_path):
    """The caller is usually a status endpoint whose job is to REPORT the
    problem, not to die of it."""
    path = tmp_path / "junk.db"
    path.write_bytes(b"this is not a database" * 500)
    try:
        h = check_health(path)
    except sqlite3.DatabaseError:
        pytest.fail("check_health() raised instead of reporting")
    assert h["ok"] is False


def test_health_is_callable_without_a_working_store(tmp_path):
    """The regression that matters. GraphStore.__init__ runs PRAGMA
    journal_mode and the schema script, both of which raise on a file damaged
    badly enough to matter - so a health check reachable only through the
    store is unreachable exactly when it is needed."""
    path = tmp_path / "junk.db"
    path.write_bytes(b"not a database at all" * 500)
    with pytest.raises(sqlite3.DatabaseError):
        GraphStore(path)                       # constructing it still fails...
    assert check_health(path)["ok"] is False   # ...but the check still answers


def test_health_does_not_modify_the_file_it_inspects(tmp_path):
    """A damaged file is the only evidence of what went wrong. Opening it
    normally would run WAL recovery and rewrite it."""
    path = _truncated_db(tmp_path)
    before = (path.stat().st_size, path.read_bytes()[:4096])
    check_health(path)
    after = (path.stat().st_size, path.read_bytes()[:4096])
    assert before == after, "inspecting the database changed it"


def test_stats_carries_the_health_verdict(tmp_path):
    """Counts without a verdict are the reassuring-but-wrong screen."""
    import inspect

    from shunkan.server import api

    src = inspect.getsource(api.create_app)
    block = src[src.index('@app.get("/api/graph")'):]
    block = block[:block.index("@app.post")]
    assert "health" in block, \
        "/api/graph returns counts with no structural verdict"

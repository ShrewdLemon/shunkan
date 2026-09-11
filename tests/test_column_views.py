"""Saved column views: the persistence endpoints, and the registry drift guard.

The drift guard is the point of this file. Everything else here is ordinary
round-tripping; `test_the_two_column_registries_agree` is the one that catches
this repo's most-repeated bug — a producer and a consumer that disagree about
one list, where both halves exist, nothing errors, and the effect is data that
is silently absent (git 39df03b: 40,000 edges written and filtered out of every
read; a45f6d7: two views reachable through one door and not the other).

app.js owns the cell renderers; columns.py validates what gets persisted. A
column id in one and not the other renders as a permanently blank column or
refuses a view the picker just offered, and neither raises.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shunkan.server import create_app
from shunkan.server import columns as colreg

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src/shunkan/server/static/app.js"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Each test gets its own APP_DIR.

    conftest points SHUNKAN_HOME at ONE temp dir for the whole session, so
    without this a view saved by one test is still on disk for the next and
    "it was already there" passes as "it round-tripped".
    """
    monkeypatch.setattr("shunkan.config.APP_DIR", tmp_path)
    with TestClient(create_app()) as c:
        yield c


def _store(tmp_path) -> dict:
    p = tmp_path / "column_views.json"
    return json.loads(p.read_text()) if p.exists() else {}


# --------------------------------------------------------------------------
# the drift guard
# --------------------------------------------------------------------------

def _parse_app_js_registry() -> dict[str, dict]:
    """Pull TABLE_COLS out of app.js by shape.

    Deliberately strict. A shape change here should go RED with a message, not
    quietly match zero columns and pass — that is failure mode (E), a test that
    asserts nothing, and it has shipped in this repo before.
    """
    src = APP_JS.read_text()
    start = src.index("const TABLE_COLS = {")
    end = src.index("\n};\n", start)
    block = src[start:end]

    out: dict[str, dict] = {}
    # Each table opens at exactly two spaces of indent: "  chain: {".
    heads = list(re.finditer(r"^  ([a-z_]+): \{$", block, re.M))
    assert heads, "TABLE_COLS no longer opens its tables as '  <name>: {'"
    for i, h in enumerate(heads):
        body = block[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(block)]
        locked = re.search(r'locked: "([a-z_0-9]+)"', body)
        default = re.search(r"def: \[([\s\S]*?)\]", body)
        assert locked and default, f"{h.group(1)}: no locked/def in TABLE_COLS"
        cols = re.findall(r'\{ id: "([a-z_0-9]+)", label: "([^"]*)"', body)
        assert cols, f"{h.group(1)}: matched no columns — the def shape changed"
        # PARTIAL drift is the dangerous case and the zero-match guard above
        # cannot see it: add one column in a brace style this regex does not
        # match and the parser silently yields N-1, every comparison below
        # still agrees, and the picker offers a tickbox the server rejects.
        # `id:` is style-independent, so a count mismatch means drift.
        declared = len(re.findall(r'\bid:\s*"', body))
        assert declared == len(cols), (
            f"{h.group(1)}: TABLE_COLS declares {declared} columns but this "
            f"parser matched {len(cols)} — a column is written in a shape the "
            f"guard cannot see, so registry drift would pass unnoticed")
        out[h.group(1)] = {
            "locked": locked.group(1),
            "default": re.findall(r'"([a-z_0-9]+)"', default.group(1)),
            "columns": cols,
        }
    return out


def test_the_two_column_registries_agree():
    """app.js renders these columns; columns.py decides what may be saved.

    Ids, labels AND order, because all three are load-bearing: an id mismatch
    refuses a view the picker offered, a label mismatch makes the API describe
    a column by a name that is not on screen, and order is what the saved list
    is re-sorted into.
    """
    js = _parse_app_js_registry()
    assert set(js) == set(colreg.TABLES), (
        f"tables differ: app.js has {sorted(js)}, columns.py has "
        f"{colreg.known_tables()}")
    for name, py in colreg.TABLES.items():
        j = js[name]
        assert [tuple(c) for c in j["columns"]] == [tuple(c) for c in py.columns], (
            f"{name}: app.js columns {j['columns']} != columns.py {list(py.columns)}")
        assert j["locked"] == py.locked, f"{name}: locked column differs"
        assert j["default"] == list(py.default), (
            f"{name}: default set differs — app.js {j['default']} vs "
            f"columns.py {list(py.default)}")


def test_every_default_can_actually_be_rendered():
    """A default naming a column the table does not have would paint one
    column fewer than the picker ticks, with nothing to say why."""
    for name, t in colreg.TABLES.items():
        assert t.default, f"{name}: empty default set"
        unknown = [c for c in t.default if c not in t.ids]
        assert not unknown, f"{name}: default names unknown columns {unknown}"
        assert t.locked in t.ids, f"{name}: locked column is not one of its columns"
        assert t.locked in t.default, f"{name}: the identity column must ship on"
        assert list(t.default) == [c for c in t.ids if c in t.default], (
            f"{name}: the default set is not in render order")


# --------------------------------------------------------------------------
# the endpoints
# --------------------------------------------------------------------------

def test_list_ships_every_table_with_its_columns(client):
    d = client.get("/api/views").json()
    assert [t["table"] for t in d["tables"]] == colreg.known_tables()
    chain = next(t for t in d["tables"] if t["table"] == "chain")
    assert chain["locked"] == "strike"
    assert [c["id"] for c in chain["columns"]] == list(colreg.TABLES["chain"].ids)
    assert chain["active"] is None and chain["views"] == {}


def test_a_saved_view_round_trips_and_becomes_active(client, tmp_path):
    r = client.post("/api/views/screener",
                    json={"name": "Risk Desk", "columns": ["above_sma50", "symbol", "rsi"]})
    assert r.status_code == 200, r.text
    # The name is narrowed and the columns come back in RENDER order, not the
    # order they were clicked in.
    assert r.json()["name"] == "risk desk"
    assert r.json()["columns"] == ["symbol", "rsi", "above_sma50"]

    got = client.get("/api/views/screener").json()
    assert got["active"] == "risk desk"
    assert got["views"]["risk desk"] == ["symbol", "rsi", "above_sma50"]
    assert _store(tmp_path)["screener"]["views"]["risk desk"] == [
        "symbol", "rsi", "above_sma50"]


def test_an_unknown_column_is_refused_and_never_reaches_disk(client, tmp_path):
    """Storing it would come back as a blank column indistinguishable from a
    column whose data is missing."""
    r = client.post("/api/views/screener",
                    json={"name": "junk", "columns": ["symbol", "ebitda_margin"]})
    assert r.status_code == 400
    assert "ebitda_margin" in r.json()["detail"]
    assert "screener has symbol" in r.json()["detail"]   # names what does exist
    assert "junk" not in json.dumps(_store(tmp_path))


def test_a_view_cannot_drop_the_column_that_names_the_row(client):
    r = client.post("/api/views/chain", json={"name": "flow", "columns": ["call_oi", "put_oi"]})
    assert r.status_code == 400
    assert "strike" in r.json()["detail"]


def test_an_empty_view_is_refused(client):
    r = client.post("/api/views/insider", json={"name": "nothing", "columns": []})
    assert r.status_code == 400
    assert "at least one column" in r.json()["detail"]


def test_the_built_in_view_name_cannot_be_shadowed(client):
    """Saving over "default" would leave no way back to the shipped columns."""
    r = client.post("/api/views/positions", json={"name": "DEFAULT", "columns": ["contract"]})
    assert r.status_code == 400
    assert "built-in" in r.json()["detail"]


def test_a_name_with_no_usable_characters_is_refused_by_name(client):
    r = client.post("/api/views/positions", json={"name": "!!!", "columns": ["contract"]})
    assert r.status_code == 400
    assert "no usable characters" in r.json()["detail"]


def test_an_unknown_table_names_the_tables_that_exist(client):
    """"unknown table" on its own reads as a defect and gets loosened until it
    accepts anything."""
    r = client.get("/api/views/nosuchtable")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "nosuchtable" in detail
    for name in colreg.known_tables():
        assert name in detail


def test_selecting_a_view_that_does_not_exist_is_refused(client):
    r = client.post("/api/views/chain/active", json={"name": "ghost"})
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_selecting_none_returns_to_the_built_in_columns(client):
    client.post("/api/views/chain", json={"name": "flow", "columns": ["strike", "call_oi", "put_oi"]})
    assert client.get("/api/views/chain").json()["active"] == "flow"
    r = client.post("/api/views/chain/active", json={"name": None})
    assert r.status_code == 200
    got = client.get("/api/views/chain").json()
    assert got["active"] is None
    assert got["views"]["flow"] == ["call_oi", "strike", "put_oi"]   # still saved


def test_deleting_the_only_view_is_allowed(client):
    """/api/layout refuses to delete its last key because a workspace with no
    layout has nothing to render. A table always has its built-in columns to
    fall back to, so the same rule here would be a control that cannot
    succeed."""
    client.post("/api/views/insider", json={"name": "who", "columns": ["person", "date"]})
    r = client.request("DELETE", "/api/views/insider", params={"name": "who"})
    assert r.status_code == 200
    got = client.get("/api/views/insider").json()
    assert got["views"] == {} and got["active"] is None


def test_deleting_a_view_that_does_not_exist_names_what_does(client):
    client.post("/api/views/insider", json={"name": "who", "columns": ["person", "date"]})
    r = client.request("DELETE", "/api/views/insider", params={"name": "nope"})
    assert r.status_code == 404
    assert "who" in r.json()["detail"]


def test_a_column_an_upgrade_removed_is_named_not_silently_dropped(client, tmp_path):
    """A view saved against an older build can name a column this one no
    longer renders. Dropping it quietly means the table paints one column
    fewer than the user saved, with nothing on screen to explain it."""
    (tmp_path / "column_views.json").write_text(json.dumps({
        "screener": {"active": "old", "views": {"old": ["symbol", "rsi", "beta_5y"]}},
    }))
    got = client.get("/api/views/screener").json()
    assert got["views"]["old"] == ["symbol", "rsi"]
    assert got["stale"]["old"] == ["beta_5y"]


def test_a_corrupt_store_names_itself_instead_of_erasing_every_view(client, tmp_path):
    """Silently returning {} was worse than failing.

    Every saved view vanished with no stated cause, and the next save
    overwrote whatever had survived - so a crash mid-write became permanent
    data loss that looked like "you never saved anything". A refusal that
    names its cause is the house rule, and the damaged file is moved aside
    rather than destroyed: it is the only record of what the user had.
    """
    store = tmp_path / "column_views.json"
    store.write_text('{"screener": {"active": "risk", "views"')      # truncated
    r = client.get("/api/views")
    assert r.status_code == 503, "a corrupt store was reported as an empty one"
    detail = r.json()["detail"]
    assert "column_views.json" in detail["error"]
    assert "JSONDecodeError" in detail["detail"]
    assert not store.exists(), "the damaged file was left in place to fail again"
    assert (tmp_path / "column_views.json.corrupt").exists(), \
        "the damaged file was destroyed rather than preserved"


def test_a_store_of_the_wrong_shape_degrades_rather_than_500ing(client, tmp_path):
    """Valid JSON of the wrong shape is a hand-edit, not a crash. One bad
    per-table entry must not take the whole picker down."""
    (tmp_path / "column_views.json").write_text(
        '{"screener": "desk", "chain": {"active": null, "views": {}}}')
    r = client.get("/api/views")
    assert r.status_code == 200, "a hand-edited entry returned a 500"
    assert all(t["views"] == {} for t in r.json()["tables"]
               if t["table"] == "screener")


def test_column_views_never_appear_in_the_workspace_layout_picker(client):
    """The reason this is a sibling store and not a corner of layouts.json:
    GET /api/layouts renders every key it finds as a selectable workspace, so
    a column view saved there would be offered as a workspace to switch to."""
    before = client.get("/api/layouts").json()["names"]
    client.post("/api/views/chain", json={"name": "flow", "columns": ["strike", "call_oi"]})
    assert client.get("/api/layouts").json()["names"] == before


def test_the_frontend_never_hides_a_column_it_already_built():
    """Rule: a column the user did not choose is not built. display:none in
    the column-view block would mean the browser holds a number the screen
    denies, and a "hidden" column also goes stale unnoticed."""
    src = APP_JS.read_text()
    # Column 0, not the doc comment above the block that names both markers:
    # slicing comment-to-comment yields a string with no cells in it at all,
    # and every assertion below would then pass on nothing.
    block = src[src.index("\nconst COLVIEW_LIFT_START = true;"):
                src.index("\nconst COLVIEW_LIFT_END = true;")]
    assert "const TABLE_COLS = {" in block, "the lift markers no longer bracket the cells"
    flat = block.lower().replace(" ", "")
    assert "display:none" not in flat
    assert "visibility:hidden" not in flat
    assert "<colgroup" not in flat, "a <col> is the other way to hide a built column"


# --------------------------------------------------------------------------
# the shipped renderers, run for real
# --------------------------------------------------------------------------

def _env() -> dict:
    """Inherit the real environment — a hand-written PATH found no node at all
    on this machine, and the skip would then have hidden the whole test."""
    return {**os.environ, "NODE_PATH": str(ROOT / "node_modules")}


COLVIEW_JS = ROOT / "tests" / "colview_render_test.js"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_column_cells_render_a_dash_and_never_a_blank_cell():
    """The cell functions themselves, over a fixture where every value is null.

    Needs no jsdom: these build strings, and building the string wrong is how
    the money bugs shipped. Also checks that an unticked column is dropped
    rather than hidden, and that the DEFAULT set is still exactly the columns
    the tables had before saved views existed.
    """
    r = subprocess.run(["node", str(COLVIEW_JS)], cwd=ROOT, capture_output=True,
                       text=True, env=_env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 failed" in r.stdout


def test_every_table_kind_has_both_a_picker_and_a_repaint():
    """The WS_WIDGETS/WS_RENDER shape of bug, pinned before it happens.

    A table in TABLE_COLS with no colviewSlot() renders a registry entry no
    user can reach; one with no colviewMount() renders a COLS button whose
    ticks change nothing on screen. Both halves exist, nothing throws, and the
    control just quietly does not work — which is how ANL_GROUPS and
    CODE_ALIAS drifted apart twice.
    """
    src = APP_JS.read_text()
    for table in colreg.known_tables():
        assert f'colviewSlot("{table}")' in src, \
            f"{table}: registered as a column view but no picker is mounted anywhere"
        assert re.search(rf'colviewMount\([^,]+, "{re.escape(table)}"', src), \
            f"{table}: has a picker but nothing repaints its table when columns change"

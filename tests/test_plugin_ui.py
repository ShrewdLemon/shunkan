import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
APP = HERE.parent / "src" / "shunkan" / "server" / "static" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_register_view_lands_in_every_table():
    out = subprocess.run(["node", str(HERE / "plugin_ui_test.js")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_unknown_view_is_refused_not_thrown():
    src = APP.read_text()
    body = src[src.index("function show(viewId"):]
    body = body[:body.index("\n}\n")]
    assert "not in this edition" in body


def test_plugins_load_before_first_render():
    src = APP.read_text()
    assert "await loadPlugins()" in src

"""Asset URLs must change when the asset changes, and only then.

index.html shipped hand-written cache busters - app.js?v=80, styles.css?v=51 -
that a human had to remember to bump. Across one session of edits nobody did,
so browsers kept serving the cached v=80 bundle. The symptoms all pointed at
the server, which was serving the correct file the whole time:

  a whole page section was missing, because that cached build predated it
  a screen hung forever on a spinner, because that build predated its error
  handling
  reloading changed nothing, because the URL had not changed

A number a human maintains is a number that goes stale.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from shunkan.server.api import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


def _versions(html: str) -> dict[str, str]:
    return dict(re.findall(r"/static/([\w.]+)\?v=([\w]+)", html))


def test_assets_are_versioned_by_content(client):
    from shunkan.server.api import STATIC_DIR

    import hashlib

    html = client.get("/").text
    got = _versions(html)
    assert "app.js" in got and "styles.css" in got, got
    for asset, ver in got.items():
        want = hashlib.md5((STATIC_DIR / asset).read_bytes()).hexdigest()[:10]
        assert ver == want, (
            f"{asset} is served as ?v={ver} but its content hashes to {want} - "
            f"browsers will keep the stale copy")


def test_no_hand_written_version_numbers_survive(client):
    """The literal ?v=80 in the file must not reach the browser."""
    html = client.get("/").text
    for asset, ver in _versions(html).items():
        assert not ver.isdigit(), (
            f"{asset} still carries a hand-maintained version (?v={ver}); "
            f"it will go stale the next time someone edits the file")


def test_version_changes_when_the_file_does(client, tmp_path):
    from shunkan.server.api import STATIC_DIR

    target = STATIC_DIR / "app.js"
    before = _versions(client.get("/").text)["app.js"]
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n// touched by a test\n")
        after = _versions(client.get("/").text)["app.js"]
    finally:
        target.write_bytes(original)
    assert after != before, "editing the asset did not change its URL"
    assert _versions(client.get("/").text)["app.js"] == before, \
        "restoring the asset did not restore its URL - the hash is not stable"


def test_index_itself_is_never_cached(client):
    """The stamps live in index.html, so a cached index pins stale stamps."""
    r = client.get("/")
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc or "no-cache" in cc, \
        f"index.html is cacheable ({cc!r}); it would freeze the asset versions"

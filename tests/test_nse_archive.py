"""nsearchives GETs: one success and two failure outcomes, judged by the body."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from shunkan.data import nse_archive
from shunkan.data.nse_archive import get, save_once
from shunkan.data.provider import DataError, FetchFailed, Unpublished

URL = nse_archive.ARCHIVE + "/content/fo/x.csv.zip"
DENIED = b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have permission"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "a,b\n1,2\n")
    return buf.getvalue()


def _client(status=200, body=b"", seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_outcomes_are_data_errors():
    assert issubclass(Unpublished, DataError) and issubclass(FetchFailed, DataError)
    assert not issubclass(Unpublished, FetchFailed) and not issubclass(FetchFailed, Unpublished)


def test_200_zip_returns_the_bytes_with_a_browser_ua():
    seen = []
    body = _zip()
    assert get(URL, "zip", client=_client(200, body, seen)) == body
    ua = seen[0].headers["user-agent"]
    assert "Chrome" in ua and "Mozilla" in ua
    assert set(nse_archive.HEADERS) == {"User-Agent", "Accept"}


def test_404_is_unpublished():
    with pytest.raises(Unpublished):
        get(URL, "zip", client=_client(404, b"Not Found"))


def test_500_is_a_failed_fetch():
    with pytest.raises(FetchFailed, match="500"):
        get(URL, "zip", client=_client(500, b"oops"))


def test_200_access_denied_html_is_failed_not_unpublished():
    with pytest.raises(FetchFailed, match="Access Denied"):
        get(URL, "csv", client=_client(200, DENIED))


def test_200_csv_without_its_header_is_failed():
    with pytest.raises(FetchFailed, match="Client Type"):
        get(URL, "csv", must_contain=b"Client Type", client=_client(200, b"a,b\n1,2\n"))


def test_200_csv_with_its_header_passes():
    body = b"title\nClient Type,x\n"
    assert get(URL, "csv", must_contain=b"Client Type", client=_client(200, body)) == body


def test_200_non_zip_for_zip_is_failed():
    with pytest.raises(FetchFailed, match="zip"):
        get(URL, "zip", client=_client(200, b"PK not really a zip"))


@pytest.mark.parametrize("expect", ["zip", "csv", "xls"])
def test_200_empty_body_is_failed(expect):
    with pytest.raises(FetchFailed):
        get(URL, expect, client=_client(200, b""))


def test_xls_needs_the_ole_magic():
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64
    assert get(URL, "xls", client=_client(200, ole)) == ole
    with pytest.raises(FetchFailed):
        get(URL, "xls", client=_client(200, DENIED))


def test_network_error_is_a_failed_fetch():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FetchFailed, match="ConnectError"):
        get(URL, "zip", client=client)


def test_unknown_expect_is_a_programming_error():
    with pytest.raises(ValueError):
        get(URL, "pdf", client=_client(200, b"%PDF"))


def test_save_once_writes_once_and_never_overwrites(tmp_path):
    p = tmp_path / "a" / "b" / "f.csv"
    assert save_once(p, b"first") == p
    assert p.read_bytes() == b"first"
    assert save_once(p, b"second") == p
    assert p.read_bytes() == b"first"
    assert [x.name for x in p.parent.iterdir()] == ["f.csv"]


def test_unknown_expect_is_rejected_before_any_request():
    seen = []
    with pytest.raises(ValueError):
        get(URL, "pdf", client=_client(200, b"%PDF", seen))
    assert seen == []


def test_save_once_syncs_before_the_rename(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = nse_archive.os.fsync, nse_archive.os.replace
    monkeypatch.setattr(nse_archive.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(nse_archive.os, "replace",
                        lambda a, b: (calls.append("replace"), real_replace(a, b)))
    save_once(tmp_path / "f.csv", b"x")
    assert calls == ["fsync", "replace"]


def test_save_once_file_is_world_readable(tmp_path):
    p = save_once(tmp_path / "f.csv", b"x")
    assert p.stat().st_mode & 0o777 == 0o644

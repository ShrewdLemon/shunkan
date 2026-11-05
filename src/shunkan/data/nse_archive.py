"""GETs against NSE's static archive host, judged by the body.

nsearchives serves past files, but a blocked request can come back as a 200
HTML "Access Denied" page, so the status alone proves nothing: 404 is
Unpublished, and anything that is not the expected file is FetchFailed.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path

import httpx

from shunkan.data.provider import FetchFailed, Unpublished

ARCHIVE = "https://nsearchives.nseindia.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
    "Accept": "*/*",
}
_OLE = b"\xd0\xcf\x11\xe0"


def _looks_like(data: bytes, expect: str) -> bool:
    if expect == "zip":
        return zipfile.is_zipfile(io.BytesIO(data))
    if expect == "csv":
        return bool(data.strip()) and not data.lstrip().startswith(b"<")
    return data.startswith(_OLE)


def get(url: str, expect: str, must_contain: bytes | None = None, client=None,
        timeout: float = 20.0) -> bytes:
    if expect not in ("zip", "csv", "xls"):
        raise ValueError(f"unknown expect {expect!r}")
    name = httpx.URL(url).path.rsplit("/", 1)[-1]
    try:
        if client is None:
            r = httpx.get(url, headers=HEADERS, timeout=timeout)
        else:
            r = client.get(url, headers=HEADERS, timeout=timeout)
    except httpx.HTTPError as exc:
        raise FetchFailed(f"{name}: {type(exc).__name__}: {exc}") from exc
    if r.status_code == 404:
        raise Unpublished(f"{name}: HTTP 404")
    if r.status_code != 200:
        raise FetchFailed(f"{name}: HTTP {r.status_code}")
    data = r.content
    if not _looks_like(data, expect):
        raise FetchFailed(f"{name}: expected {expect}, got {len(data)} bytes {data[:60]!r}")
    if must_contain is not None and must_contain not in data:
        raise FetchFailed(f"{name}: no {must_contain!r} in {data[:60]!r}")
    return data


def save_once(path: Path, data: bytes) -> Path:
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path

"""The one polite client for www.nseindia.com.

Akamai fronts this host: a burst gets the IP blocked for days, and a block can
come back as a 200 HTML "Access Denied". So every request, the homepage
warm-up included, takes an fcntl lock shared across processes and waits until
MIN_GAP_S after the last one. A blocked answer opens a breaker on disk for
BREAKER_S, during which calls raise without touching the network. The warm-up
status is ignored: the homepage can be 403 while the API answers 200 JSON.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import httpx

from shunkan.config import CACHE_DIR, ensure_dirs
from shunkan.data.nse_archive import HEADERS
from shunkan.data.provider import FetchFailed, Unpublished
from shunkan.markets import IST

BASE = "https://www.nseindia.com"
MIN_GAP_S = 10.0
BREAKER_S = 1800.0
_API_HEADERS = {"Accept": "application/json", "Referer": BASE + "/"}


class Client:
    def __init__(self, state_dir: Path | None = None, clock=time.time, sleep=time.sleep,
                 transport=None):
        if state_dir is None:
            ensure_dirs()
            state_dir = CACHE_DIR / "nse_www"
        self.state_dir = Path(state_dir)
        self._clock = clock
        self._sleep = sleep
        self._http = httpx.Client(transport=transport, headers=HEADERS, timeout=20.0)
        self._warm = False

    def get_json(self, path: str, params: dict | None = None) -> dict | list:
        self._refuse_if_open()
        with self._locked():
            self._refuse_if_open()
            if not self._warm:
                self._warm_up()
            url = BASE + "/" + path.lstrip("/")
            r = self._send(url, params, _API_HEADERS)
            if r.status_code in (401, 403):
                self._warm_up()
                r = self._send(url, params, _API_HEADERS)
                if r.status_code in (401, 403):
                    self._open_breaker()
                    raise FetchFailed(f"blocked: HTTP {r.status_code} after re-warm ({path})")
            if r.status_code == 404:
                raise Unpublished(f"{path}: HTTP 404")
            if r.status_code == 429 or (r.status_code != 200 and r.content.lstrip().startswith(b"<")):
                self._open_breaker()
                raise FetchFailed(f"blocked: HTTP {r.status_code} {r.text.strip()[:60]}".rstrip())
            if r.status_code != 200:
                raise FetchFailed(f"{path}: HTTP {r.status_code}")
            try:
                data = json.loads(r.content)
            except ValueError:
                self._open_breaker()
                raise FetchFailed(f"blocked: {r.text.strip()[:60]}") from None
            if not isinstance(data, (dict, list)):
                raise FetchFailed(f"{path}: JSON {type(data).__name__}, not an object or array")
            return data

    def _warm_up(self) -> None:
        self._send(BASE + "/")
        self._warm = True

    def _send(self, url: str, params=None, headers=None) -> httpx.Response:
        last = _read(self.state_dir / "last")
        if last is not None:
            wait = min(MIN_GAP_S, last + MIN_GAP_S - self._clock())
            if wait > 0:
                self._sleep(wait)
        _write(self.state_dir / "last", self._clock())
        try:
            return self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise FetchFailed(f"{httpx.URL(url).path}: {type(exc).__name__}: {exc}") from exc
        finally:
            _write(self.state_dir / "last", self._clock())

    def _refuse_if_open(self) -> None:
        until = _read(self.state_dir / "breaker")
        if until is not None and self._clock() < until:
            hhmm = datetime.fromtimestamp(until, IST).strftime("%H:%M")
            raise FetchFailed(f"breaker open until {hhmm} IST")

    def _open_breaker(self) -> None:
        _write(self.state_dir / "breaker", self._clock() + BREAKER_S)
        self._warm = False

    @contextmanager
    def _locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _read(path: Path) -> float | None:
    try:
        return float(path.read_text())
    except (OSError, ValueError):
        return None


def _write(path: Path, value: float) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(repr(value))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_shared: Client | None = None
_shared_lock = threading.Lock()


def get_json(path: str, params: dict | None = None) -> dict | list:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Client()
        client = _shared
    return client.get_json(path, params)

"""Broker API adapters: Zerodha Kite Connect and Groww.

Both are credential-gated REST APIs. Shunkan reads credentials from
~/.shunkan/credentials.json (or environment variables) and uses whichever
broker is configured for real-time quotes; everything else falls back to
free sources automatically.

credentials.json:
{
  "zerodha": {"api_key": "xxx", "access_token": "yyy"},
  "groww":   {"api_token": "zzz"}
}

Env overrides: KITE_API_KEY / KITE_ACCESS_TOKEN, GROWW_API_TOKEN.

NOTE: these adapters follow the brokers' published REST formats but can
only be exercised for real once you add your own credentials — both APIs
are paid/auth-gated. Run `shunkan connect` for setup instructions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from shunkan.config import APP_DIR
from shunkan.data.provider import DataError, Quote

CREDENTIALS_FILE = APP_DIR / "credentials.json"


def load_credentials() -> dict:
    creds: dict = {}
    if CREDENTIALS_FILE.exists():
        try:
            creds = json.loads(CREDENTIALS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            creds = {}
    z = creds.setdefault("zerodha", {})
    z.setdefault("api_key", os.environ.get("KITE_API_KEY", ""))
    z.setdefault("access_token", os.environ.get("KITE_ACCESS_TOKEN", ""))
    g = creds.setdefault("groww", {})
    g.setdefault("api_token", os.environ.get("GROWW_API_TOKEN", ""))
    return creds


def save_credentials(section: str, **fields: str) -> None:
    """Merge fields into one broker's section of credentials.json (0600)."""
    from shunkan.config import ensure_dirs

    ensure_dirs()
    creds: dict = {}
    if CREDENTIALS_FILE.exists():
        try:
            creds = json.loads(CREDENTIALS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            creds = {}
    creds.setdefault(section, {}).update({k: v for k, v in fields.items() if v})
    # Write to a 0600 temp file in the same directory and rename over the
    # target. write_text-then-chmod published api_secret and access_token at
    # the process umask for the width of a syscall, and left a half-written
    # file behind if the process died mid-write.
    tmp = CREDENTIALS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(creds, fh, indent=2)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, CREDENTIALS_FILE)
    try:
        CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Zerodha daily login flow
# ---------------------------------------------------------------------------

KITE_REDIRECT_PORT = 8722  # Redirect URL in your Kite app must be
KITE_REDIRECT_PATH = "/callback"  # http://127.0.0.1:8722/callback


def kite_login_flow(
    api_key: str, api_secret: str, port: int = KITE_REDIRECT_PORT, timeout: float = 300.0
) -> str:
    """Run the Kite Connect daily login: open the browser, catch the
    request_token on the local redirect, exchange it for an access_token,
    persist everything. Returns the access_token.

    Access tokens are invalidated by Zerodha every morning (~6 AM IST), so
    this runs once per trading day: `shunkan connect zerodha`.
    """
    import hashlib
    import http.server
    import urllib.parse
    import webbrowser

    holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib API)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = query.get("request_token", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if token:
                holder["request_token"] = token
                self.wfile.write(
                    b"<h2>Shunkan: login captured.</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                )
            else:
                self.wfile.write(b"<h2>Shunkan: no request_token in redirect.</h2>")

        def log_message(self, *args):  # silence per-request stderr noise
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1.0

    login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
    webbrowser.open(login_url)
    print(f"Waiting for Kite login at {login_url}")
    print(f"(listening on http://127.0.0.1:{port}{KITE_REDIRECT_PATH} — "
          "your app's Redirect URL must match)")

    import time as _time

    deadline = _time.monotonic() + timeout
    while "request_token" not in holder and _time.monotonic() < deadline:
        server.handle_request()
    server.server_close()

    request_token = holder.get("request_token")
    if not request_token:
        raise DataError("Login timed out — no request_token received.")

    checksum = hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode()
    ).hexdigest()
    resp = httpx.post(
        "https://api.kite.trade/session/token",
        data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        headers={"X-Kite-Version": "3"},
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise DataError(f"Token exchange failed (HTTP {resp.status_code}). "
                        "Check the api_secret and the redirect URL on your Kite app.")
    access_token = resp.json().get("data", {}).get("access_token", "")
    if not access_token:
        raise DataError("Kite accepted the request but returned no access_token.")

    save_credentials(
        "zerodha", api_key=api_key, api_secret=api_secret, access_token=access_token
    )
    return access_token


def kite_login_url(api_key: str) -> str:
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


def kite_catch_and_exchange(
    api_key: str, api_secret: str, port: int = KITE_REDIRECT_PORT,
    timeout: float = 300.0,
) -> str:
    """Headless half of the daily login: listen for the redirect, exchange
    the request_token, persist. The caller opens the login URL (browser tab
    from the web app, webbrowser from the CLI) — the user always types their
    credentials on Zerodha's own page, never here."""
    import hashlib
    import http.server
    import time as _time
    import urllib.parse

    holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib API)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = query.get("request_token", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if token:
                holder["request_token"] = token
                self.wfile.write(b"<h2>Shunkan: login captured.</h2>"
                                 b"<p>You can close this tab - the terminal "
                                 b"reconnects itself.</p>")
            else:
                self.wfile.write(b"<h2>Shunkan: no request_token in redirect.</h2>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1.0
    deadline = _time.monotonic() + timeout
    while "request_token" not in holder and _time.monotonic() < deadline:
        server.handle_request()
    server.server_close()

    request_token = holder.get("request_token")
    if not request_token:
        raise DataError("Login timed out — no request_token received.")
    checksum = hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    resp = httpx.post(
        "https://api.kite.trade/session/token",
        data={"api_key": api_key, "request_token": request_token,
              "checksum": checksum},
        headers={"X-Kite-Version": "3"}, timeout=10.0,
    )
    if resp.status_code != 200:
        raise DataError(f"Token exchange failed (HTTP {resp.status_code}). "
                        "Check the api_secret and the redirect URL on your Kite app.")
    access_token = resp.json().get("data", {}).get("access_token", "")
    if not access_token:
        raise DataError("Kite accepted the request but returned no access_token.")
    save_credentials("zerodha", api_key=api_key, api_secret=api_secret,
                     access_token=access_token)
    return access_token


class KiteProvider:
    """Zerodha Kite Connect v3 — real-time quotes for NSE cash + NFO."""

    BASE = "https://api.kite.trade"

    def set_token(self, api_key: str, access_token: str) -> None:
        """Hot-swap a fresh daily token into the live session — no restart."""
        self._client.headers["Authorization"] = f"token {api_key}:{access_token}"

    def healthy(self) -> tuple[bool, str]:
        """One cheap REST probe. 403 = the daily token has expired — the WS
        may still stream from an earlier login, so 'connected' and 'healthy'
        are different claims and the UI must not conflate them."""
        try:
            r = self._client.get("/quote", params=[("i", "NSE:NIFTY 50")])
            if r.status_code == 403:
                return False, "daily access token expired (Kite invalidates every morning)"
            r.raise_for_status()
            return True, ""
        except Exception as exc:
            return False, f"Kite REST unreachable: {str(exc)[:120]}"

    def __init__(self, api_key: str, access_token: str) -> None:
        if not api_key or not access_token:
            raise DataError(
                "Zerodha not configured. Add api_key + access_token to "
                f"{CREDENTIALS_FILE} (see `shunkan connect`)."
            )
        self._client = httpx.Client(
            base_url=self.BASE,
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {api_key}:{access_token}",
            },
            timeout=5.0,
        )

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        instruments = [f"NSE:{s.upper().removesuffix('.NS')}" for s in symbols]
        try:
            resp = self._client.get("/quote", params=[("i", i) for i in instruments])
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except Exception as exc:
            raise DataError(f"Kite quote failed: {exc}") from exc

        out: dict[str, Quote] = {}
        for inst, payload in data.items():
            sym = inst.split(":", 1)[-1]
            last = float(payload.get("last_price") or 0.0)
            ohlc = payload.get("ohlc") or {}
            prev = float(ohlc.get("close") or 0.0)
            out[sym] = Quote(
                symbol=sym,
                price=last,
                change=last - prev,
                change_pct=(last / prev - 1.0) if prev else 0.0,
                volume=int(payload.get("volume") or 0),
                prev_close=prev,
                day_high=float(ohlc.get("high") or 0.0) or None,
                day_low=float(ohlc.get("low") or 0.0) or None,
            )
        if not out:
            raise DataError("Kite returned no quotes")
        return out

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[symbol.upper().removesuffix(".NS")]

    # -- raw access used by the F&O module --------------------------------

    def get_json(self, path: str, params=None) -> dict:
        try:
            resp = self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise DataError(f"Kite GET {path} failed: {exc}") from exc

    def get_text(self, path: str) -> str:
        try:
            resp = self._client.get(path)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            raise DataError(f"Kite GET {path} failed: {exc}") from exc

    def post_json(self, path: str, body) -> dict:
        """POST a JSON body. Used by the basket-margin endpoint, which is the
        only way to price a multi-leg F&O position without guessing."""
        try:
            resp = self._client.post(path, json=body, timeout=15.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise DataError(f"Kite POST {path} failed: {exc}") from exc


class GrowwProvider:
    """Groww trading API — live quotes for NSE cash and derivatives."""

    BASE = "https://api.groww.in"

    def __init__(self, api_token: str) -> None:
        if not api_token:
            raise DataError(
                "Groww not configured. Add api_token to "
                f"{CREDENTIALS_FILE} (see `shunkan connect`)."
            )
        self._client = httpx.Client(
            base_url=self.BASE,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            },
            timeout=5.0,
        )

    def quote(self, symbol: str) -> Quote:
        sym = symbol.upper().removesuffix(".NS")
        try:
            resp = self._client.get(
                "/v1/live-data/quote",
                params={"exchange": "NSE", "segment": "CASH", "trading_symbol": sym},
            )
            resp.raise_for_status()
            payload = resp.json().get("payload", {})
        except Exception as exc:
            raise DataError(f"Groww quote failed: {exc}") from exc

        last = float(payload.get("last_price") or 0.0)
        ohlc = payload.get("ohlc") or {}
        prev = float(ohlc.get("close") or payload.get("prev_close") or 0.0)
        return Quote(
            symbol=sym,
            price=last,
            change=last - prev,
            change_pct=(last / prev - 1.0) if prev else 0.0,
            volume=int(payload.get("volume") or 0),
            prev_close=prev,
            day_high=float(ohlc.get("high") or 0.0) or None,
            day_low=float(ohlc.get("low") or 0.0) or None,
        )

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for s in symbols:
            try:
                q = self.quote(s)
                out[q.symbol] = q
            except DataError:
                continue
        if not out:
            raise DataError("Groww returned no quotes")
        return out


def get_broker():
    """Return the configured broker provider, or None when not set up."""
    creds = load_credentials()
    z = creds.get("zerodha", {})
    if z.get("api_key") and z.get("access_token"):
        return KiteProvider(z["api_key"], z["access_token"])
    g = creds.get("groww", {})
    if g.get("api_token"):
        return GrowwProvider(g["api_token"])
    return None


CONNECT_HELP = f"""\
Connecting your broker gives Shunkan true real-time quotes (free sources
are delayed). Either broker works; Zerodha also unlocks F&O depth later.

ZERODHA (Kite Connect, https://developers.kite.trade):
  1. Create an app at developers.kite.trade — type "Connect".
     · App name: anything (e.g. Shunkan)
     · Zerodha Client ID: your own Kite login ID
     · Redirect URL: http://127.0.0.1:{KITE_REDIRECT_PORT}{KITE_REDIRECT_PATH}   <- must match exactly
     · Postback URL: leave blank
  2. Run:  shunkan connect zerodha
     Paste the api_key + api_secret once; a browser opens for Kite login
     and Shunkan captures + exchanges the token automatically.
  3. Access tokens expire every morning (~6 AM IST) — rerun
     `shunkan connect zerodha` each trading day (key/secret are remembered).

GROWW (https://groww.in/trade-api):
  1. Generate an API token in your Groww account settings.
  2. Run:  shunkan connect groww --token YOUR_TOKEN

Credentials are stored in {CREDENTIALS_FILE} (chmod 600).
Environment variables also work: KITE_API_KEY, KITE_ACCESS_TOKEN, GROWW_API_TOKEN.
Without a broker, Shunkan uses Yahoo Finance (delayed) and NSE public data.
"""

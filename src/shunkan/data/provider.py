"""Market data providers.

- YFinanceProvider: live data from Yahoo Finance with a small on-disk cache
  so repeated lookups inside a session are instant.
- SyntheticProvider: deterministic geometric-Brownian-motion data with regime
  shifts, used for offline/demo mode (SHUNKAN_OFFLINE=1) and for tests.

Both expose the same interface: history(), quote(), quotes(), news().
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import numpy as np
import pandas as pd

from shunkan.config import CACHE_DIR, ensure_dirs
from shunkan.markets import denormalize_symbol, normalize_symbol

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

VALID_PERIODS = ["1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"]
VALID_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"]


class DataError(RuntimeError):
    """Raised when market data cannot be fetched."""


@dataclass
class Quote:
    symbol: str
    price: float
    change: float  # absolute change vs previous close
    change_pct: float  # fractional, e.g. 0.0123
    volume: int
    prev_close: float
    day_high: float | None = None
    day_low: float | None = None
    market_cap: float | None = None
    name: str | None = None


class DataProvider(Protocol):
    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame: ...
    def quote(self, symbol: str) -> Quote: ...
    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def news(self, symbol: str, limit: int = 10) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------


class YFinanceProvider:
    """Yahoo Finance data with a parquet cache under ~/.shunkan/cache.

    Daily-or-coarser history is cached for 15 minutes; intraday for 1 minute.
    """

    def __init__(self) -> None:
        ensure_dirs()

    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        """India-first: 'RELIANCE' resolves to RELIANCE.NS, 'NIFTY' to ^NSEI.
        If the .NS-normalized form has no data, the raw symbol is retried so
        US tickers like AAPL keep working without a suffix."""
        raw = symbol.upper()
        symbol = normalize_symbol(raw)
        cached = self._read_cache(symbol, period, interval)
        if cached is not None:
            return cached

        import yfinance as yf

        try:
            df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
        except Exception as exc:  # network, parsing, etc.
            raise DataError(f"Failed to fetch {symbol}: {exc}") from exc
        if (df is None or df.empty) and symbol == f"{raw}.NS":
            try:
                df = yf.Ticker(raw).history(period=period, interval=interval, auto_adjust=True)
                symbol = raw
            except Exception:
                df = None
        if df is None or df.empty:
            raise DataError(f"No data returned for '{raw}' (period={period}, interval={interval})")

        df = df.rename(columns={c: c.lower() for c in df.columns})
        df = df[[c for c in OHLCV_COLUMNS if c in df.columns]]
        # Drop bars with no close (e.g. today's not-yet-settled bar can be NaN).
        df = df.dropna(subset=["close"])
        self._write_cache(symbol, period, interval, df)
        return df

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[symbol.upper()]

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        import yfinance as yf

        out: dict[str, Quote] = {}
        # display name -> yahoo ticker (keys in the result use display names)
        mapping = {s.upper(): normalize_symbol(s) for s in symbols}
        try:
            tickers = yf.Tickers(" ".join(mapping.values()))
            for display, ticker in mapping.items():
                try:
                    info = tickers.tickers[ticker].fast_info
                    price = float(info["lastPrice"])
                    prev = float(info["previousClose"])
                    out[display] = Quote(
                        symbol=display,
                        price=price,
                        change=price - prev,
                        change_pct=(price / prev - 1.0) if prev else 0.0,
                        volume=int(info.get("lastVolume") or 0),
                        prev_close=prev,
                        day_high=_maybe_float(info, "dayHigh"),
                        day_low=_maybe_float(info, "dayLow"),
                        market_cap=_maybe_float(info, "marketCap"),
                        name=denormalize_symbol(ticker),
                    )
                except Exception:
                    continue
        except Exception as exc:
            raise DataError(f"Quote fetch failed: {exc}") from exc
        missing = [s for s in mapping if s not in out]
        if missing and not out:
            raise DataError(f"No quotes returned for {', '.join(missing)}")
        return out

    def news(self, symbol: str, limit: int = 10) -> list[dict]:
        import yfinance as yf

        try:
            raw = yf.Ticker(normalize_symbol(symbol)).news or []
        except Exception as exc:
            raise DataError(f"News fetch failed for {symbol}: {exc}") from exc
        items = []
        for entry in raw[:limit]:
            content = entry.get("content", entry)
            items.append(
                {
                    "title": content.get("title", "(no title)"),
                    "publisher": (content.get("provider") or {}).get("displayName", "")
                    if isinstance(content.get("provider"), dict)
                    else content.get("publisher", ""),
                    "link": _news_link(content),
                    "published": content.get("pubDate") or content.get("providerPublishTime", ""),
                    "summary": content.get("summary", ""),
                }
            )
        return items

    # -- cache ------------------------------------------------------------

    def _cache_path(self, symbol: str, period: str, interval: str):
        key = hashlib.sha1(f"{symbol}:{period}:{interval}".encode()).hexdigest()[:16]
        return CACHE_DIR / f"{symbol}_{key}.parquet"

    def _cache_ttl(self, interval: str) -> float:
        return 60.0 if interval.endswith(("m", "h")) else 900.0

    def _read_cache(self, symbol: str, period: str, interval: str) -> pd.DataFrame | None:
        path = self._cache_path(symbol, period, interval)
        try:
            if path.exists() and time.time() - path.stat().st_mtime < self._cache_ttl(interval):
                return pd.read_parquet(path)
        except Exception:
            pass
        return None

    def _write_cache(self, symbol: str, period: str, interval: str, df: pd.DataFrame) -> None:
        try:
            df.to_parquet(self._cache_path(symbol, period, interval))
        except Exception:
            pass  # cache is best-effort; pyarrow may be absent


def _maybe_float(info, key: str) -> float | None:
    try:
        v = info[key]
        return float(v) if v is not None else None
    except Exception:
        return None


def _news_link(content: dict) -> str:
    for key in ("canonicalUrl", "clickThroughUrl"):
        v = content.get(key)
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
        if isinstance(v, str) and v:
            return v
    return content.get("link", "")


# ---------------------------------------------------------------------------
# Synthetic (offline/demo/tests)
# ---------------------------------------------------------------------------


class SyntheticProvider:
    """Deterministic synthetic OHLCV via GBM with regime shifts.

    The seed derives from the symbol so 'AAPL' always produces the same
    series — useful for demos, offline work, and reproducible tests.
    """

    PERIOD_BARS = {
        "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252,
        "2y": 504, "5y": 1260, "10y": 2520, "max": 5040,
    }

    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        symbol = symbol.upper()
        n = self.PERIOD_BARS.get(period, 252)
        seed = int(hashlib.sha1(symbol.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)

        start_price = 20.0 + (seed % 480)
        # Random regime lengths with different drift/vol per regime.
        drifts, vols = [], []
        remaining = n
        while remaining > 0:
            length = int(rng.integers(20, 120))
            length = min(length, remaining)
            drifts += [rng.normal(0.0004, 0.0008)] * length
            vols += [abs(rng.normal(0.015, 0.006)) + 0.005] * length
            remaining -= length
        drift = np.array(drifts)
        vol = np.array(vols)

        shocks = rng.standard_normal(n)
        log_ret = drift - 0.5 * vol**2 + vol * shocks
        close = start_price * np.exp(np.cumsum(log_ret))

        spread = vol * close
        open_ = np.empty(n)
        open_[0] = start_price
        open_[1:] = close[:-1] * (1 + rng.normal(0, 0.002, n - 1))
        high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.4, n)) * spread
        low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.4, n)) * spread
        volume = (rng.lognormal(15, 0.5, n)).astype(np.int64)

        end = datetime.now()
        idx = pd.bdate_range(end=end, periods=n)
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=idx,
        )

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[symbol.upper()]

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            sym = sym.upper()
            hist = self.history(sym, period="3mo")
            price = float(hist["close"].iloc[-1])
            prev = float(hist["close"].iloc[-2])
            out[sym] = Quote(
                symbol=sym,
                price=price,
                change=price - prev,
                change_pct=price / prev - 1.0,
                volume=int(hist["volume"].iloc[-1]),
                prev_close=prev,
                day_high=float(hist["high"].iloc[-1]),
                day_low=float(hist["low"].iloc[-1]),
                market_cap=price * 1_000_000_000,
                name=f"{sym} (synthetic)",
            )
        return out

    def news(self, symbol: str, limit: int = 10) -> list[dict]:
        now = datetime.now()
        headlines = [
            "{s} announces quarterly results, beats analyst expectations",
            "Analysts raise price target on {s} after product launch",
            "{s} expands into new markets amid strong demand",
            "Institutional investors increase positions in {s}",
            "{s} faces supply chain questions; management remains confident",
        ]
        return [
            {
                "title": h.format(s=symbol.upper()),
                "publisher": "Shunkan Wire (offline demo)",
                "link": "",
                "published": (now - timedelta(hours=3 * i)).isoformat(),
                "summary": "Offline demo headline — set SHUNKAN_OFFLINE=0 for live news.",
            }
            for i, h in enumerate(headlines[:limit])
        ]


class BrokerFirstProvider:
    """Real-time broker quotes when available, Yahoo for everything else.

    Quote calls try the broker first and silently fall back to Yahoo per
    call (so an expired Kite token degrades the data, not the terminal).
    History and news always come from the free sources — brokers don't
    improve them enough to justify burning API credits.
    """

    def __init__(self, broker, fallback: "YFinanceProvider") -> None:
        self.broker = broker
        self.fallback = fallback

    @property
    def broker_name(self) -> str:
        return type(self.broker).__name__.removesuffix("Provider")

    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        return self.fallback.history(symbol, period, interval)

    def news(self, symbol: str, limit: int = 10) -> list[dict]:
        return self.fallback.news(symbol, limit)

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[symbol.upper()]

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        # Indices (^NSEI, INR=X …) aren't NSE cash instruments — Yahoo only.
        brokerable = [s for s in symbols if not any(c in normalize_symbol(s) for c in "^=")]
        out: dict[str, Quote] = {}
        if brokerable:
            try:
                got = self.broker.quotes(brokerable)
                for s in brokerable:
                    key = s.upper().removesuffix(".NS")
                    if key in got:
                        q = got[key]
                        q.symbol = s.upper()
                        out[s.upper()] = q
            except DataError:
                pass  # broker down/expired token — Yahoo covers below
        remaining = [s for s in symbols if s.upper() not in out]
        if remaining:
            try:
                out.update(self.fallback.quotes(remaining))
            except DataError:
                if not out:
                    raise
        return out


def is_offline() -> bool:
    return os.environ.get("SHUNKAN_OFFLINE", "").strip() in {"1", "true", "yes"}


def get_provider() -> DataProvider:
    """Offline -> synthetic. Broker configured -> broker-first composite.
    Otherwise plain Yahoo."""
    if is_offline():
        return SyntheticProvider()
    yf_provider = YFinanceProvider()
    try:
        from shunkan.data.brokers import get_broker

        broker = get_broker()
    except Exception:
        broker = None
    if broker is not None:
        return BrokerFirstProvider(broker, yf_provider)
    return yf_provider

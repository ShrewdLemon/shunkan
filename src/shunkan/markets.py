"""Market metadata: Indian symbol normalization and IST trading sessions.

Shunkan is India-first: bare symbols ("RELIANCE") map to NSE listings
("RELIANCE.NS" for Yahoo), and index names ("NIFTY", "BANKNIFTY") map to
their data-source tickers. Symbols with explicit suffixes/prefixes
(AAPL has none but is whitelisted via fallback, ^GSPC, BTC-USD, RELIANCE.NS)
pass through untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Friendly name -> Yahoo ticker for Indian indices and macro instruments.
INDEX_ALIASES: dict[str, str] = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MIDCAP_100.NS",
    "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX",
    "VIX": "^INDIAVIX",
    "USDINR": "INR=X",
}

# Pulse board: what a derivatives trader glances at before anything else.
INDIA_PULSE: list[tuple[str, str]] = [
    ("NIFTY 50", "^NSEI"),
    ("BANK NIFTY", "^NSEBANK"),
    ("SENSEX", "^BSESN"),
    ("INDIA VIX", "^INDIAVIX"),
    ("USD/INR", "INR=X"),
]

GLOBAL_PULSE: list[tuple[str, str]] = [
    ("S&P 500", "^GSPC"),
    ("NASDAQ", "^IXIC"),
    ("DOW", "^DJI"),
    ("FTSE 100", "^FTSE"),
    ("DAX", "^GDAXI"),
    ("NIKKEI 225", "^N225"),
    ("HANG SENG", "^HSI"),
    ("BRENT", "BZ=F"),
    ("GOLD", "GC=F"),
    ("US 10Y", "^TNX"),
]

DEFAULT_INDIA_WATCHLIST = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "BHARTIARTL", "LT", "BAJFINANCE", "NIFTY", "BANKNIFTY",
]

# NSE F&O index underlyings (for option-chain commands).
FNO_INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


# NSE listings whose official symbol contains a hyphen — these must NOT be
# treated as explicit tickers (BTC-USD, BRK-B) or they lose their .NS suffix.
NSE_HYPHENATED = {"BAJAJ-AUTO", "NAM-INDIA", "MCDOWELL-N", "GMRP&UI"}


def normalize_symbol(symbol: str) -> str:
    """Map a user-typed symbol to its Yahoo Finance ticker (India-first)."""
    sym = symbol.strip().upper()
    if sym in INDEX_ALIASES:
        return INDEX_ALIASES[sym]
    if sym in NSE_HYPHENATED:
        return f"{sym}.NS"
    # Explicit tickers pass through: ^GSPC, RELIANCE.NS, INR=X, BTC-USD, GC=F
    if any(ch in sym for ch in (".", "^", "=", "-")):
        return sym
    return f"{sym}.NS"


def denormalize_symbol(ticker: str) -> str:
    """Friendly display name for a normalized ticker."""
    for name, t in INDEX_ALIASES.items():
        if t == ticker:
            return name
    return ticker.removesuffix(".NS")


@dataclass
class SessionInfo:
    phase: str  # one of PHASES
    is_open: bool
    description: str


# IST cash-session phases. F&O hours match the cash market.
PHASES = (
    "pre_open",      # 09:00–09:15 auction
    "opening",       # 09:15–10:00 high-vol discovery
    "midday",        # 10:00–14:30
    "closing",       # 14:30–15:30 MIS square-off pressure
    "post_market",   # 15:30–18:00
    "overnight",     # everything else
)


def session_phase(when: datetime | None = None) -> SessionInfo:
    """Classify a moment into an IST market-session phase."""
    now = (when or datetime.now(tz=IST)).astimezone(IST)
    t = now.time()
    weekday = now.weekday() < 5  # Mon-Fri; exchange holidays not modeled

    def info(phase: str, is_open: bool, desc: str) -> SessionInfo:
        return SessionInfo(phase=phase, is_open=is_open, description=desc)

    if not weekday:
        return info("overnight", False, "Weekend — next session Monday 09:15 IST")
    if time(9, 0) <= t < time(9, 15):
        return info("pre_open", False, "Pre-open auction — gap forming")
    if time(9, 15) <= t < time(10, 0):
        return info("opening", True, "Opening hour — highest volatility window")
    if time(10, 0) <= t < time(14, 30):
        return info("midday", True, "Midday session")
    if time(14, 30) <= t < time(15, 30):
        return info("closing", True, "Closing hour — intraday square-off flows")
    if time(15, 30) <= t < time(18, 0):
        return info("post_market", False, "Post-market — news lands on tomorrow's open")
    return info("overnight", False, "Overnight — global cues set tomorrow's gap")


# ---------------------------------------------------------------------------
# Expiry clock — NSE F&O contracts stop trading at the 15:30 IST cash close.
# Every option price in the app dates itself from here, so there is exactly
# one clock and it is never the host machine's.
# ---------------------------------------------------------------------------

MARKET_CLOSE = time(15, 30)
SECONDS_PER_YEAR = 365.0 * 24 * 3600  # calendar years — NSE's own IV convention

# Floor on time to expiry. Under ~1s the IV solve reads the last residual
# premium as volatility and pins to implied_vol()'s 5.0 clamp — a fabricated
# 500% that renders as real — and ATM gamma/theta (both scaling as 1/sqrt(T))
# blow up with it. Five minutes keeps the last session's numbers sane and
# only ever binds after 15:25 on expiry day.
MIN_TTE_SECONDS = 300.0


def now_ist(when: datetime | None = None) -> datetime:
    """IST-aware 'now'. Pass tz-aware datetimes; naive ones read as host-local."""
    return (when or datetime.now(tz=IST)).astimezone(IST)


def today_ist(when: datetime | None = None) -> date:
    """The current trading date in IST, whatever timezone the host runs in."""
    return now_ist(when).date()


def expiry_close(expiry: date) -> datetime:
    """The 15:30 IST moment a contract expiring on `expiry` settles."""
    return datetime.combine(expiry, MARKET_CLOSE, tzinfo=IST)


def is_expired(expiry: date, when: datetime | None = None) -> bool:
    """True once that expiry's 15:30 IST bell has rung — the contract is dead.

    Callers must consult this before rendering: `time_to_expiry_years` floors
    at MIN_TTE_SECONDS to keep the Black-Scholes solve finite, so on its own
    it would report a settled contract as having five minutes of life left.
    """
    return now_ist(when) >= expiry_close(expiry)


def time_to_expiry_years(expiry: date, when: datetime | None = None) -> float:
    """Years to the 15:30 IST expiry close, floored at MIN_TTE_SECONDS.

    Calendar time, not trading time: seconds tick overnight and across
    weekends exactly as they do intraday, which is the convention the quoted
    IVs use. Trading holidays are deliberately not modeled — they shift T by
    well under the bid-ask spread's worth of vega, and a wrong holiday list
    would be a worse error than none.

    The floor keeps the solve finite; it does not mean the contract is alive.
    Pair with `is_expired` at every render site.
    """
    seconds = (expiry_close(expiry) - now_ist(when)).total_seconds()
    return max(seconds, MIN_TTE_SECONDS) / SECONDS_PER_YEAR


# ---------------------------------------------------------------------------
# World exchange sessions — the session globe's data source.
# Regular cash hours only; lunch breaks modeled where they exist (Tokyo,
# Hong Kong). Exchange holidays are not modeled, and the UI says so.
# ---------------------------------------------------------------------------

@dataclass
class Exchange:
    code: str
    city: str
    lat: float
    lon: float
    tz: str
    windows: tuple[tuple[time, time], ...]  # local open/close pairs


EXCHANGES: tuple[Exchange, ...] = (
    Exchange("NSE", "Mumbai", 19.07, 72.88, "Asia/Kolkata",
             ((time(9, 15), time(15, 30)),)),
    Exchange("LSE", "London", 51.51, -0.09, "Europe/London",
             ((time(8, 0), time(16, 30)),)),
    Exchange("NYSE", "New York", 40.71, -74.01, "America/New_York",
             ((time(9, 30), time(16, 0)),)),
    Exchange("XETRA", "Frankfurt", 50.11, 8.68, "Europe/Berlin",
             ((time(9, 0), time(17, 30)),)),
    Exchange("TSE", "Tokyo", 35.68, 139.70, "Asia/Tokyo",
             ((time(9, 0), time(11, 30)), (time(12, 30), time(15, 0)))),
    Exchange("HKEX", "Hong Kong", 22.28, 114.16, "Asia/Hong_Kong",
             ((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0)))),
    Exchange("SGX", "Singapore", 1.35, 103.82, "Asia/Singapore",
             ((time(9, 0), time(17, 0)),)),
    Exchange("ASX", "Sydney", -33.87, 151.21, "Australia/Sydney",
             ((time(10, 0), time(16, 0)),)),
)


def world_sessions(when: datetime | None = None) -> list[dict]:
    """Open/closed state for every tracked exchange at `when` (UTC-aware).

    Pure timezone math via zoneinfo — DST handled by the tz database.
    """
    now_utc = when or datetime.now(tz=ZoneInfo("UTC"))
    out = []
    for ex in EXCHANGES:
        local = now_utc.astimezone(ZoneInfo(ex.tz))
        weekday = local.weekday() < 5
        t = local.time()
        in_window = any(a <= t < b for a, b in ex.windows)
        is_open = weekday and in_window
        lunch = (weekday and len(ex.windows) == 2
                 and ex.windows[0][1] <= t < ex.windows[1][0])
        out.append({
            "code": ex.code, "city": ex.city, "lat": ex.lat, "lon": ex.lon,
            "open": is_open,
            "state": "open" if is_open else ("lunch" if lunch else "closed"),
            "local_time": local.strftime("%H:%M"),
            "local_day": local.strftime("%a"),
            "hours": " · ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in ex.windows),
        })
    return out

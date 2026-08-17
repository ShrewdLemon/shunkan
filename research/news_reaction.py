"""Do shocks with named news behave differently from shocks without?

Run: .venv/bin/python research/news_reaction.py

The first study the news archive enables, pre-registered here before the
numbers were seen:

H1 (information vs liquidity): a down-shock accompanied by negative named
news continues; a down-shock with NO named news reverses. This is the classic
result (news-driven moves are repriced information, no-news moves are
liquidity) and Indian large caps may or may not show it.

H2: the SIGN of same-day news sentiment predicts the sign of the next few
days' excess move.

Design, and its honest limits:
- Events: per-company |z| >= 2 days (trailing 63d vol, lagged one day),
  refractory 5 trading days, inside the news archive's coverage window.
- Join: title-tagged headlines dated t-1 or t (timestamps are date-granular).
- Sentiment: the keyword scorer on titles. Crude, deterministic, stated.
- Outcome: EXCESS log return vs NIFTY at +1, +3, +5 closes, so a stock that
  merely fell with the market and bounced with it shows nothing.
- Pooled across companies for n. Shocks cluster on market-wide days and
  sector co-movement survives the excess adjustment, so pooled t-stats are
  overstated to that extent; the count of DISTINCT event dates is printed
  next to n so the reader can see how much independence there really is.
- The backfill news channel is a sample of what Google indexes today, so
  "no named news" partly means "none indexed now". That biases AGAINST H1's
  no-news bucket being clean, and is stated rather than hidden.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from shunkan.analytics.events import shock_days
from shunkan.data.newsstore import store_file
from shunkan.store.store import STORE_DIR

HORIZONS = (1, 3, 5)
REFRACTORY = 5
SENT_EPS = 0.03      # |mean sentiment| below this is "neutral"


def t_stat(x: np.ndarray) -> float | None:
    n = len(x)
    if n < 3:
        return None
    sd = x.std(ddof=1)
    return float(x.mean() / (sd / math.sqrt(n))) if sd > 1e-12 else None


def main() -> None:
    news = pd.read_parquet(store_file())
    news = news[news.symbols != ""].copy()
    news["ts"] = pd.to_datetime(news["ts"], utc=True, errors="coerce")
    news["day"] = news["ts"].dt.date
    lo = news["ts"].min()
    covered = set(news["query_symbol"][news.origin == "backfill"].unique())

    bench = (pd.read_parquet(STORE_DIR / "history" / "_NSEI.parquet")
             .sort_values("date").set_index("date")["close"])
    bench.index = pd.DatetimeIndex(bench.index)
    lbench = np.log(bench)

    rows = []
    used_symbols = 0
    for sym in sorted(covered):
        f = STORE_DIR / "history" / f"{sym}.parquet"
        if not f.exists():
            continue
        close = (pd.read_parquet(f).sort_values("date")
                 .set_index("date")["close"])
        close.index = pd.DatetimeIndex(close.index)
        joint = pd.concat({"s": np.log(close), "b": lbench}, axis=1).dropna()
        if len(joint) < 260:
            continue
        used_symbols += 1
        tagged = news[news.symbols.str.contains(
            r"(?:^|\s)" + sym + r"(?:\s|$)", regex=True)]
        by_day = tagged.groupby("day")["sentiment"].mean()

        events = [d for d in shock_days(close, sigma=2.0, direction="any")
                  if pd.Timestamp(d, tz="UTC") >= lo and d in joint.index]
        # refractory, in this symbol's trading days
        kept, last = [], None
        pos_of = {ts: i for i, ts in enumerate(joint.index)}
        for d in events:
            i = pos_of[d]
            if last is None or i - last > REFRACTORY:
                kept.append(d)
                last = i
        for d in kept:
            i = pos_of[d]
            if i + max(HORIZONS) >= len(joint):
                continue
            r_day = float(joint["s"].iloc[i] - joint["s"].iloc[i - 1])
            sents = [by_day.get((d - pd.Timedelta(days=k)).date())
                     for k in (0, 1)]
            sents = [s for s in sents if s is not None and not np.isnan(s)]
            sent = float(np.mean(sents)) if sents else None
            row = {"symbol": sym, "date": d.date(), "direction":
                   "down" if r_day < 0 else "up",
                   "news": "none" if sent is None
                   else "neg" if sent < -SENT_EPS
                   else "pos" if sent > SENT_EPS else "neutral"}
            for h in HORIZONS:
                ex = ((joint["s"].iloc[i + h] - joint["s"].iloc[i])
                      - (joint["b"].iloc[i + h] - joint["b"].iloc[i]))
                row[f"ex{h}"] = float(ex) * 100
            rows.append(row)

    ev = pd.DataFrame(rows)
    print(f"symbols used: {used_symbols}   pooled shock events: {len(ev)}   "
          f"distinct dates: {ev['date'].nunique()}")
    print(f"news window: {lo.date()} onward   sentiment eps: ±{SENT_EPS}\n")

    def table(sub: pd.DataFrame, label: str) -> None:
        print(label)
        base = {h: sub[f"ex{h}"].to_numpy() for h in HORIZONS}
        print(f"  {'bucket':10s} {'n':>4} {'dates':>6}"
              + "".join(f" {'+%dd mean' % h:>9} {'t':>6}" for h in HORIZONS))
        line = f"  {'ALL':10s} {len(sub):>4} {sub['date'].nunique():>6}"
        for h in HORIZONS:
            t = t_stat(base[h])
            line += f" {base[h].mean():>8.2f}% {t if t is None else format(t, '>6.2f')!s:>6}"
        print(line)
        for bucket in ("neg", "neutral", "pos", "none"):
            g = sub[sub.news == bucket]
            if not len(g):
                continue
            line = f"  {bucket:10s} {len(g):>4} {g['date'].nunique():>6}"
            for h in HORIZONS:
                x = g[f"ex{h}"].to_numpy()
                t = t_stat(x)
                line += f" {x.mean():>8.2f}% {t if t is None else format(t, '>6.2f')!s:>6}"
            print(line)
        print()

    table(ev[ev.direction == "down"], "DOWN-SHOCKS (excess vs NIFTY after)")
    table(ev[ev.direction == "up"], "UP-SHOCKS (excess vs NIFTY after)")

    print("read with: pooled events cluster on market days (see distinct dates);")
    print("'none' partly means 'not indexed by Google today', which biases against H1.")


if __name__ == "__main__":
    main()

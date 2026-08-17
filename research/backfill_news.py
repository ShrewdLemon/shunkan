"""Resume the one-year news backfill for the NIFTY50+BANKNIFTY universe.

Run: .venv/bin/python research/backfill_news.py

Safe to interrupt and re-run at any point: persistence is per weekly window
and dedup is on title+date, so a re-run refetches cheaply and stores nothing
twice. Progress state is the archive itself, read at startup, so this script
skips any symbol that already has backfill rows and continues with the rest.
"""

import pandas as pd

from shunkan.data.constituents import alias_table, universe
from shunkan.data.newsstore import backfill_symbol, store_file

done: set[str] = set()
f = store_file()
if f.exists():
    df = pd.read_parquet(f)
    done = set(df[df.origin == "backfill"]["query_symbol"].unique())
    print(f"archive: {len(df):,} rows; already backfilled: {len(done)} symbols")

uni = universe()
aliases = alias_table(uni)
todo = [c for c in uni if c.symbol not in done]
print(f"remaining: {len(todo)} of {len(uni)}")
for i, c in enumerate(todo, 1):
    r = backfill_symbol(c.symbol, c.name, weeks=52, aliases=aliases)
    print(f"[{i}/{len(todo)}] {r['symbol']}: +{r['added']} rows, "
          f"{r['failed_windows']} failed windows", flush=True)
print("DONE")

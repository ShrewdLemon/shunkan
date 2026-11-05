"""Command-line entry for the tick recorder.

    python -m shunkan.algo record --symbols NIFTY26SEPFUT,NIFTY2690124200CE
    python -m shunkan.algo record --tokens 12030210 --until 15:45
    python -m shunkan.algo sessions
    python -m shunkan.algo inspect 2026-08-25
    python -m shunkan.algo paper --symbols NIFTY26SEPFUT --watch-chain 10
    python -m shunkan.algo compare 2026-08-25-paper

Deliberately a module entry rather than a `shunkan` subcommand: this lives on
a feature branch, and adding to `cli.py` would collide with concurrent work
there for no benefit. Fold it into the main CLI at merge time if you want it.

Run it from a machine that will stay awake. A recording with a hole in it is
worse than no recording, because the hole is invisible in the parquet and
only shows up as a strategy that behaves differently live.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from datetime import datetime, time as dtime

from shunkan.algo.recorder import TICKS_DIR, TickRecorder
from shunkan.algo.replay import session_meta
from shunkan.algo.risk import RiskLimits


def _resolve(symbols: list[str]) -> dict[str, int]:
    """Symbol -> instrument token via the daily Kite instruments dump.

    The dump is served unauthenticated, so this works before you have a
    broker session for the day.
    """
    from shunkan.data.kite_fno import load_instruments

    out: dict[str, int] = {}
    wanted = {s.strip().upper() for s in symbols if s.strip()}
    for exchange in ("NFO", "NSE"):
        if not wanted:
            break
        try:
            df = load_instruments(None, exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {exchange} instruments unavailable: {exc}", file=sys.stderr)
            continue
        hit = df[df["tradingsymbol"].str.upper().isin(wanted)]
        for _, row in hit.iterrows():
            out[str(row["tradingsymbol"]).upper()] = int(row["instrument_token"])
        wanted -= set(out)
    if wanted:
        print(f"  ! unresolved: {', '.join(sorted(wanted))}", file=sys.stderr)
    return out


def _seconds_until(hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = datetime.combine(now.date(), dtime(hour=h, minute=m))
    return max(0.0, (target - now).total_seconds())


def cmd_record(args: argparse.Namespace) -> int:
    from shunkan.data.brokers import load_credentials

    creds = load_credentials().get("zerodha", {})
    api_key, access_token = creds.get("api_key"), creds.get("access_token")
    if not api_key or not access_token:
        print("No Zerodha session. Run `shunkan connect zerodha` first.", file=sys.stderr)
        return 1

    tokens: list[int] = []
    if args.tokens:
        tokens += [int(t) for t in args.tokens.split(",") if t.strip()]
    if args.symbols:
        resolved = _resolve(args.symbols.split(","))
        for sym, tok in sorted(resolved.items()):
            print(f"  {sym:<28} {tok}")
        tokens += list(resolved.values())
    tokens = sorted(set(tokens))
    if not tokens:
        print("Nothing to record: pass --tokens or --symbols.", file=sys.stderr)
        return 1

    duration = _seconds_until(args.until) if args.until else args.duration
    if duration is not None and duration <= 0:
        print(f"--until {args.until} is already past.", file=sys.stderr)
        return 1

    root = TICKS_DIR
    rec = TickRecorder(tokens=tokens, root=root, session=args.session)
    try:
        rec.start()
    except FileExistsError as exc:
        print(f"Refusing to record: {exc}", file=sys.stderr)
        return 1
    print(f"recording {len(tokens)} instrument(s) in FULL mode -> {root}")
    print(f"duration: {'until interrupted' if duration is None else f'{duration:,.0f}s'}")

    from shunkan.stream.ticker import KiteTicker

    ticker = KiteTicker(api_key, access_token)
    ticker.start(tokens, rec.on_ticks, mode="full")
    started = time.time()
    try:
        while duration is None or (time.time() - started) < duration:
            time.sleep(5.0)
            s = rec.stats
            el = time.time() - started
            rate = s.ticks_received / el if el else 0.0
            print(
                f"\r  {el:>7.0f}s  ticks {s.ticks_received:>10,}  "
                f"written {s.ticks_written:>10,}  dropped {s.ticks_dropped:>6,}  "
                f"{rate:>6.0f}/s  {s.bytes_written/1e6:>7.1f} MB",
                end="", flush=True,
            )
    except KeyboardInterrupt:
        print("\n  interrupted — flushing")
    finally:
        ticker.stop()
        stats = rec.stop()
    print(f"\n\nsession {rec.session}")
    print(json.dumps(stats.as_dict(), indent=2))
    if stats.ticks_dropped:
        print(
            f"\nWARNING: dropped {stats.ticks_dropped:,} ticks "
            f"({stats.drop_rate:.2%}). This recording has holes; treat any "
            f"backtest over it as suspect.",
            file=sys.stderr,
        )
    return 0


def cmd_sessions(_: argparse.Namespace) -> int:
    if not TICKS_DIR.is_dir():
        print(f"no recordings under {TICKS_DIR}")
        return 0
    rows = sorted(d.name for d in TICKS_DIR.iterdir() if d.is_dir())
    if not rows:
        print(f"no recordings under {TICKS_DIR}")
        return 0
    print(f"{'session':<14}{'ticks':>12}{'dropped':>10}{'parts':>7}{'size':>10}")
    for name in rows:
        m = session_meta(name)
        size = sum(p.stat().st_size for p in (TICKS_DIR / name).glob("*.parquet"))
        print(
            f"{name:<14}{m.get('ticks_written', 0):>12,}"
            f"{m.get('ticks_dropped', 0):>10,}{m.get('parts', 0):>7}"
            f"{size/1e6:>9.1f}M"
        )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from collections import Counter

    from shunkan.algo.book import is_tradeable
    from shunkan.algo.replay import replay

    n = 0
    per_token: Counter = Counter()
    untradeable = 0
    spreads: list[float] = []
    first = last = None
    for ev in replay(args.session):
        n += 1
        per_token[ev.tick.token] += 1
        if not is_tradeable(ev.tick):
            untradeable += 1
        else:
            spreads.append(ev.tick.spread)
        first = first or ev.ts_ns
        last = ev.ts_ns
    if not n:
        print("empty session")
        return 1
    span = (last - first) / 1e9
    spreads.sort()
    print(f"session      {args.session}")
    print(f"ticks        {n:,}")
    print(f"span         {span:,.0f}s  ({n/span if span else 0:,.0f}/s)")
    print(f"instruments  {len(per_token)}")
    for tok, c in per_token.most_common():
        print(f"   {tok:<12} {c:>10,}")
    print(f"untradeable  {untradeable:,} ({untradeable/n:.2%})  <- crossed/one-sided books")
    if spreads:
        print(f"spread p50   {spreads[len(spreads)//2]:.2f}")
        print(f"spread p90   {spreads[int(len(spreads)*0.9)]:.2f}")
    return 0



# --------------------------------------------------------------------------
# paper trading
# --------------------------------------------------------------------------

def _load_strategy(spec: str, params: dict | None = None):
    """`quote-both-sides`, `null`, or `package.module:ClassName`."""
    from shunkan.algo.strategy import build_strategy

    return build_strategy(spec, params)


def _limits_from(args: argparse.Namespace) -> RiskLimits:
    kw: dict = {}
    if args.max_loss is not None:
        kw["max_daily_loss"] = args.max_loss
    if args.max_drawdown is not None:
        kw["max_drawdown"] = args.max_drawdown
    if args.max_position is not None:
        kw["max_position"] = args.max_position
    if args.max_order_qty is not None:
        kw["max_order_qty"] = args.max_order_qty
    if args.max_gross_exposure is not None:
        kw["max_gross_exposure"] = args.max_gross_exposure
    if args.max_orders_per_day is not None:
        kw["max_orders_per_day"] = args.max_orders_per_day
    if args.max_orders_per_minute is not None:
        kw["max_orders_per_minute"] = args.max_orders_per_minute
    if args.max_orders_per_second is not None:
        kw["max_orders_per_second"] = args.max_orders_per_second
    if args.close_at:
        h, m = (int(x) for x in args.close_at.split(":"))
        kw["session_close"] = dtime(hour=h, minute=m)
    if args.kill_file:
        kw["kill_file"] = args.kill_file
    return RiskLimits(**kw)


def cmd_paper(args: argparse.Namespace) -> int:
    from shunkan.algo.charges import Segment
    from shunkan.algo.paper import PaperConfig, PaperTrader, save_report
    from shunkan.algo.risk import RiskGovernor
    from shunkan.algo.strategy import strategy_params
    from shunkan.data.brokers import load_credentials

    if args.until and _seconds_until(args.until) <= 0:
        print(f"--until {args.until} is already past.", file=sys.stderr)
        return 1
    session = args.session or (datetime.now().strftime("%Y-%m-%d") + "-paper")
    try:
        TickRecorder(root=TICKS_DIR, session=session).refuse_existing()
    except FileExistsError as exc:
        print(f"Refusing to paper trade: {exc}", file=sys.stderr)
        return 1

    creds = load_credentials().get("zerodha", {})
    api_key, access_token = creds.get("api_key"), creds.get("access_token")
    if not api_key or not access_token:
        print("No Zerodha session. Run `shunkan connect zerodha` first.", file=sys.stderr)
        return 1

    tokens: list[int] = []
    resolved: dict[str, int] = {}
    if args.tokens:
        tokens += [int(t) for t in args.tokens.split(",") if t.strip()]
    if args.symbols:
        resolved = _resolve(args.symbols.split(","))
        for sym, tok in sorted(resolved.items()):
            print(f"  {sym:<28} {tok}")
        tokens += list(resolved.values())
    tokens = sorted(set(tokens))
    if not tokens:
        print("Nothing to trade: pass --tokens or --symbols.", file=sys.stderr)
        return 1
    if len(tokens) > 1:
        print(f"Paper takes one traded token; got {len(tokens)} "
              f"({', '.join(map(str, tokens))}): the engine keeps one aggregate position.",
              file=sys.stderr)
        return 1
    segment = Segment(args.segment or (
        "future" if resolved and all(s.endswith("FUT") for s in resolved) else "option"
    ))
    watch: list[int] = []
    if args.watch_chain:
        from shunkan.algo.chain import select_chain
        from shunkan.data.kite_fno import load_instruments

        try:
            center = args.chain_center
            if center is None:
                from shunkan.data.brokers import KiteProvider

                center = KiteProvider(api_key, access_token).quote("NIFTY 50").price
            chain = select_chain(load_instruments(None, exchange="NFO"), center,
                                 args.watch_chain, datetime.now().date())
        except Exception as exc:  # noqa: BLE001
            print(f"  ! cannot resolve the NIFTY chain to watch ({exc}); "
                  "trading with no watch", file=sys.stderr)
        else:
            watch = [t for t in chain if t not in tokens]
            print(f"  watching {len(watch)} token(s): NIFTY chain +/-{args.watch_chain} "
                  f"around {center:,.2f}, NIFTY 50, India VIX")

    strategy = _load_strategy(args.strategy)
    try:
        params = strategy_params(strategy)
    except ValueError as exc:
        print(f"Cannot record this strategy for replay: {exc}", file=sys.stderr)
        return 1
    limits = _limits_from(args)
    governor = RiskGovernor(limits)

    print()
    print("PAPER MODE — simulated fills against the live book. No orders are sent.")
    print(f"  strategy      {getattr(strategy, 'name', type(strategy).__name__)}")
    print(f"  session       {session}")
    print(f"  segment       {segment.value}")
    print(f"  max loss      {limits.max_daily_loss:,.0f}")
    print(f"  max drawdown  {limits.max_drawdown:,.0f}")
    print(f"  max position  {limits.max_position:,}")
    print(f"  max gross     {limits.max_gross_exposure:,.0f}")
    print(f"  order caps    {limits.max_orders_per_second}/s  {limits.max_orders_per_minute}/min  "
          f"{limits.max_orders_per_day:,}/day")
    print(f"  window        {limits.session_open:%H:%M}-{limits.session_close:%H:%M}")
    if limits.kill_file:
        print(f"  kill file     {limits.kill_file}  (create it to stop new orders)")
    print()

    pt = PaperTrader(
        strategy,
        PaperConfig(tokens=tokens, session=session, record=True, watch=watch,
                    segment=segment),
        governor=governor,
    )
    duration = _seconds_until(args.until) if args.until else args.duration
    result = pt.run_until(api_key, access_token, duration)

    print()
    print(result.summary())
    print()
    print(governor.summary())
    report = TICKS_DIR / f"{session}.report.json"
    save_report(result, governor, report, strategy_spec=args.strategy,
                strategy_params=params, tokens=tokens, watch=watch, segment=segment,
                engine_config=pt.engine.cfg)
    print(f"\nreport -> {report}")
    print(f"tape   -> {TICKS_DIR / session}")
    print(f"\nNow replay it and compare:\n  python -m shunkan.algo compare {session}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Replay a paper session with its own setup, diff it, and log the gap."""
    from shunkan.algo.paper import append_gap, replay_gap

    report_path = TICKS_DIR / f"{args.session}.report.json"
    if not report_path.exists():
        print(f"no paper report at {report_path}", file=sys.stderr)
        return 1
    try:
        gap = replay_gap(args.session, root=TICKS_DIR)
    except (TypeError, ImportError, ValueError) as exc:
        print(f"cannot replay {args.session}: {exc}", file=sys.stderr)
        return 1
    print(f"session {args.session}\n")
    print(gap.summary())
    print()
    print("Paper and replay share one engine and one fill model: this gap measures "
          "determinism and setup, not fill realism.")
    ledger = TICKS_DIR / "gaps.jsonl"
    append_gap(gap, ledger)
    print(f"\nledger -> {ledger}")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shunkan.algo", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="Record full-depth ticks to parquet")
    r.add_argument("--tokens", help="Comma-separated instrument tokens")
    r.add_argument("--symbols", help="Comma-separated tradingsymbols (NFO/NSE)")
    r.add_argument("--until", help="Stop at local HH:MM, e.g. 15:45")
    r.add_argument("--duration", type=float, help="Stop after N seconds")
    r.add_argument("--session", help="Recording name; default today's date")
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("sessions", help="List recorded sessions")
    s.set_defaults(func=cmd_sessions)

    i = sub.add_parser("inspect", help="Summarise a recorded session")
    i.add_argument("session")
    i.set_defaults(func=cmd_inspect)

    pa = sub.add_parser("paper", help="Paper trade live (no orders are sent)")
    pa.add_argument("--tokens")
    pa.add_argument("--symbols")
    pa.add_argument("--strategy", default="quote-both-sides")
    pa.add_argument("--session")
    pa.add_argument("--until", help="Stop at local HH:MM")
    pa.add_argument("--duration", type=float, help="Stop after N seconds")
    pa.add_argument("--max-loss", type=float, dest="max_loss")
    pa.add_argument("--max-drawdown", type=float, dest="max_drawdown")
    pa.add_argument("--max-position", type=int, dest="max_position")
    pa.add_argument("--max-order-qty", type=int, dest="max_order_qty")
    pa.add_argument("--max-gross-exposure", type=float, dest="max_gross_exposure",
                    help="Rupees of |position| x price; the default is option-sized")
    pa.add_argument("--max-orders-per-day", type=int, dest="max_orders_per_day")
    pa.add_argument("--max-orders-per-minute", type=int, dest="max_orders_per_minute")
    pa.add_argument("--max-orders-per-second", type=int, dest="max_orders_per_second")
    pa.add_argument("--close-at", dest="close_at", help="Stop sending orders at HH:MM")
    pa.add_argument("--kill-file", type=Path, dest="kill_file",
                    help="Create this file to stop new orders")
    pa.add_argument("--segment", choices=["option", "future"],
                    help="Charges basis; default future when every symbol is a FUT")
    pa.add_argument("--watch-chain", type=int, dest="watch_chain", metavar="N",
                    help="Also record the nearest NIFTY weekly at +/-N strikes, NIFTY 50, VIX")
    pa.add_argument("--chain-center", type=float, dest="chain_center", metavar="PRICE",
                    help="Centre strike for --watch-chain; default NIFTY 50 last price")
    pa.set_defaults(func=cmd_paper)

    c = sub.add_parser("compare", help="Replay a paper session and log the gap")
    c.add_argument("session")
    c.set_defaults(func=cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

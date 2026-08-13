"""Shunkan command-line interface.

`shunkan` with no arguments launches the TUI. Subcommands give scriptable
access to the same engines:

    shunkan quote AAPL MSFT
    shunkan chart NVDA --period 1y
    shunkan backtest AAPL --strategy sma_cross --param fast=10 --param slow=100
    shunkan optimize TSLA --strategy ema_cross --metric sharpe
    shunkan screen tech "rsi<35" above_sma200
    shunkan strategies
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

console = Console()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shunkan",
        description="Shunkan (瞬間) — the all-in-one trading terminal.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Launch the interactive terminal (default)")

    p_serve = sub.add_parser("serve", help="Launch the Shunkan web terminal")
    p_serve.add_argument("--port", type=int, default=8720)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--no-browser", action="store_true")

    p_quote = sub.add_parser("quote", help="Print quotes for symbols")
    p_quote.add_argument("symbols", nargs="+")

    p_chart = sub.add_parser("chart", help="Render a chart in the terminal")
    p_chart.add_argument("symbol")
    p_chart.add_argument("--period", default="6mo")
    p_chart.add_argument("--interval", default="1d")

    p_bt = sub.add_parser("backtest", help="Run a backtest")
    p_bt.add_argument("symbol")
    p_bt.add_argument("--strategy", "-s", default="sma_cross")
    p_bt.add_argument("--period", default="5y")
    p_bt.add_argument(
        "--param", "-p", action="append", default=[],
        help="Strategy parameter as name=value (repeatable)",
    )
    p_bt.add_argument("--cash", type=float, default=10_000.0)
    p_bt.add_argument("--commission", type=float, default=0.0005)
    p_bt.add_argument("--no-short", action="store_true", help="Long/flat only")

    p_opt = sub.add_parser("optimize", help="Grid-search strategy parameters")
    p_opt.add_argument("symbol")
    p_opt.add_argument("--strategy", "-s", default="sma_cross")
    p_opt.add_argument("--metric", "-m", default="sharpe")
    p_opt.add_argument("--period", default="5y")
    p_opt.add_argument("--top", type=int, default=10)

    p_wf = sub.add_parser("walkforward", help="Walk-forward validation (anti-overfit)")
    p_wf.add_argument("symbol")
    p_wf.add_argument("--strategy", "-s", default="sma_cross")
    p_wf.add_argument("--metric", "-m", default="sharpe")
    p_wf.add_argument("--period", default="10y")
    p_wf.add_argument("--windows", type=int, default=4)

    p_pay = sub.add_parser("payoff", help="Options strategy payoff analysis")
    p_pay.add_argument("symbol")
    p_pay.add_argument("spec", nargs="+",
                       help="Strategy name (iron_condor, short_straddle, …) or legs (+23200CE -23400CE)")
    p_pay.add_argument("--width", type=int, default=2, help="Strikes from ATM for wings")

    p_iv = sub.add_parser("iv", help="Volatility analytics (IV vs realized, cone)")
    p_iv.add_argument("symbol", nargs="?", default="NIFTY")

    p_mc = sub.add_parser("montecarlo", help="Bootstrap confidence bands for a backtest")
    p_mc.add_argument("symbol")
    p_mc.add_argument("--strategy", "-s", default="sma_cross")
    p_mc.add_argument("--period", default="5y")
    p_mc.add_argument("--paths", type=int, default=2000)

    p_scr = sub.add_parser("screen", help="Screen a universe with rules")
    p_scr.add_argument("universe")
    p_scr.add_argument("rules", nargs="*")
    p_scr.add_argument("--period", default="1y")

    sub.add_parser("strategies", help="List built-in strategies")

    sub.add_parser("pulse", help="India + global market pulse with news bias")

    p_chain = sub.add_parser("chain", help="Option chain with OI analytics")
    p_chain.add_argument("symbol", nargs="?", default="NIFTY")
    p_chain.add_argument("--strikes", type=int, default=15, help="Rows around ATM")

    p_vol = sub.add_parser("vol", help="Volume pattern analysis")
    p_vol.add_argument("symbol")
    p_vol.add_argument("--period", default="6mo")

    p_news = sub.add_parser("news", help="News with sentiment + impact calls")
    p_news.add_argument("symbol", nargs="?", default=None)
    p_news.add_argument("--limit", type=int, default=12)

    p_conn = sub.add_parser("connect", help="Connect a broker (Zerodha/Groww) or show status")
    p_conn.add_argument("broker", nargs="?", choices=["zerodha", "groww"],
                        help="Run the login/setup flow for this broker")
    p_conn.add_argument("--api-key", default=None, help="Zerodha api_key")
    p_conn.add_argument("--api-secret", default=None, help="Zerodha api_secret")
    p_conn.add_argument("--token", default=None, help="Groww api_token")

    args = parser.parse_args(argv)

    try:
        if args.command in (None, "tui"):
            from shunkan.ui.app import ShunkanApp

            ShunkanApp().run()
            return 0
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "quote":
            return cmd_quote(args)
        if args.command == "chart":
            return cmd_chart(args)
        if args.command == "backtest":
            return cmd_backtest(args)
        if args.command == "optimize":
            return cmd_optimize(args)
        if args.command == "walkforward":
            return cmd_walkforward(args)
        if args.command == "payoff":
            return cmd_payoff(args)
        if args.command == "iv":
            return cmd_iv(args)
        if args.command == "montecarlo":
            return cmd_montecarlo(args)
        if args.command == "screen":
            return cmd_screen(args)
        if args.command == "strategies":
            return cmd_strategies()
        if args.command == "pulse":
            return cmd_pulse()
        if args.command == "chain":
            return cmd_chain(args)
        if args.command == "vol":
            return cmd_vol(args)
        if args.command == "news":
            return cmd_news(args)
        if args.command == "connect":
            return cmd_connect(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 1
    return 0


def _provider():
    from shunkan.data.provider import get_provider

    return get_provider()


def _pct(value: float) -> str:
    color = "green" if value >= 0 else "red"
    return f"[{color}]{value:+.2%}[/{color}]"


def cmd_serve(args) -> int:
    import threading
    import webbrowser

    import uvicorn

    from shunkan.server import create_app

    url = f"http://{args.host}:{args.port}"
    console.print(f"[bold yellow]Shunkan web terminal[/bold yellow] → {url}")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_quote(args) -> int:
    quotes = _provider().quotes(args.symbols)
    table = Table(title="Quotes", header_style="bold yellow")
    for col in ("Symbol", "Last", "Chg", "Chg%", "Volume"):
        table.add_column(col, justify="right")
    for sym in args.symbols:
        q = quotes.get(sym.upper())
        if q is None:
            table.add_row(sym.upper(), "n/a", "", "", "")
            continue
        chg_color = "green" if q.change >= 0 else "red"
        table.add_row(
            f"[bold]{q.symbol}[/bold]",
            f"{q.price:,.2f}",
            f"[{chg_color}]{q.change:+,.2f}[/{chg_color}]",
            _pct(q.change_pct),
            f"{q.volume:,}",
        )
    console.print(table)
    return 0


def cmd_chart(args) -> int:
    import plotext as plt

    hist = _provider().history(args.symbol, period=args.period, interval=args.interval)
    plt.theme("dark")
    x = list(range(len(hist)))
    try:
        plt.candlestick(
            x,
            {
                "Open": hist["open"].tolist(),
                "Close": hist["close"].tolist(),
                "High": hist["high"].tolist(),
                "Low": hist["low"].tolist(),
            },
        )
    except Exception:
        plt.plot(x, hist["close"].tolist())
    import numpy as np

    ticks = np.linspace(0, len(hist) - 1, num=min(6, len(hist)), dtype=int)
    plt.xticks(ticks.tolist(), [hist.index[i].strftime("%Y-%m-%d") for i in ticks])
    last = float(hist["close"].iloc[-1])
    chg = last / float(hist["close"].iloc[0]) - 1.0
    plt.title(f"{args.symbol.upper()} {args.period} · last {last:,.2f} ({chg:+.2%})")
    plt.show()
    return 0


def _parse_params(pairs: list[str]) -> dict:
    params = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"--param expects name=value, got '{pair}'")
        value = float(raw)
        params[key] = int(value) if value == int(value) and "." not in raw else value
    return params


def cmd_backtest(args) -> int:
    from shunkan.backtest import BacktestConfig, get_strategy, run_backtest

    strategy = get_strategy(args.strategy)
    params = _parse_params(args.param)
    hist = _provider().history(args.symbol, period=args.period, interval="1d")
    config = BacktestConfig(
        initial_cash=args.cash, commission=args.commission, allow_short=not args.no_short
    )
    result = run_backtest(
        hist,
        strategy.signal(hist, **params),
        config,
        symbol=args.symbol,
        strategy_name=strategy.name,
        params={**strategy.defaults, **params},
    )
    bench = run_backtest(
        hist, get_strategy("buy_hold").signal(hist), config,
        symbol=args.symbol, strategy_name="buy_hold",
    )

    table = Table(
        title=f"Backtest · {args.symbol.upper()} · {strategy.name} · {args.period}",
        header_style="bold yellow", show_header=False,
    )
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    for label, value in result.summary_rows():
        table.add_row(label, value)
    table.add_row("Buy & hold", f"{bench.total_return:+.2%}")
    alpha = result.total_return - bench.total_return
    table.add_row("Edge vs B&H", _pct(alpha))
    console.print(table)
    return 0


def cmd_optimize(args) -> int:
    from shunkan.backtest import get_strategy, grid_search

    strategy = get_strategy(args.strategy)
    hist = _provider().history(args.symbol, period=args.period, interval="1d")
    opt = grid_search(hist, strategy, metric=args.metric, symbol=args.symbol)

    console.print(
        f"[yellow]{opt.combos_tested}[/yellow] backtests in "
        f"[yellow]{opt.elapsed_s*1000:.0f} ms[/yellow] "
        f"({opt.combos_tested / max(opt.elapsed_s, 1e-9):,.0f}/sec)"
    )
    table = Table(
        title=f"Top {args.top} by {opt.metric} · {args.symbol.upper()} · {strategy.name}",
        header_style="bold yellow",
    )
    cols = list(opt.table.columns)
    for col in cols:
        table.add_column(col, justify="right")
    for _, row in opt.table.head(args.top).iterrows():
        cells = []
        for col in cols:
            v = row[col]
            if col in ("total_return", "cagr", "max_drawdown", "win_rate"):
                cells.append(f"{v:+.2%}" if col != "win_rate" else f"{v:.1%}")
            elif col in ("sharpe", "sortino"):
                cells.append(f"{v:.2f}")
            elif col == "num_trades":
                cells.append(f"{v:.0f}")
            else:
                cells.append(f"{v:g}")
        table.add_row(*cells)
    console.print(table)
    console.print(f"Best: [bold yellow]{opt.best_params}[/bold yellow]")
    return 0


def cmd_walkforward(args) -> int:
    from shunkan.backtest import get_strategy, walk_forward

    strategy = get_strategy(args.strategy)
    hist = _provider().history(args.symbol, period=args.period, interval="1d")
    wf = walk_forward(
        hist, strategy, metric=args.metric, n_windows=args.windows, symbol=args.symbol
    )
    table = Table(
        title=f"Walk-forward · {args.symbol.upper()} · {strategy.name} · {len(wf.windows)} windows",
        header_style="bold yellow",
    )
    for col in ("Test from", "IS Sharpe", "OOS Sharpe", "OOS Ret", "Params"):
        table.add_column(col, justify="right")
    for w in wf.windows:
        table.add_row(
            str(w.test_start)[:10], f"{w.is_sharpe:+.2f}", f"{w.oos_sharpe:+.2f}",
            _pct(w.oos_return), str(w.best_params),
        )
    console.print(table)
    console.print(
        f"OOS return {_pct(wf.oos_return)} · OOS Sharpe [bold]{wf.oos_sharpe:.2f}[/bold] · "
        f"OOS max DD {wf.oos_max_dd:.2%} · efficiency [bold]{wf.efficiency:.0%}[/bold] · "
        f"param stability {wf.param_stability:.0%}"
    )
    color = "green" if "robust" in wf.verdict else "yellow" if "fragile" in wf.verdict else "red"
    console.print(f"[bold {color}]Verdict: {wf.verdict}[/bold {color}]")
    return 0


def cmd_payoff(args) -> int:
    from shunkan.data.chains import get_chain
    from shunkan.derivatives import (
        PAYOFF_STRATEGIES,
        analyze_payoff,
        build_strategy,
        parse_custom_legs,
    )

    chain = get_chain(args.symbol)
    spec = args.spec
    if len(spec) == 1 and spec[0].lower().replace("-", "_") in PAYOFF_STRATEGIES:
        a = build_strategy(chain, spec[0], width=args.width)
    else:
        a = analyze_payoff(chain, parse_custom_legs(chain, spec), name="custom")

    def money(v):
        if v == float("inf"):
            return "[green]unlimited[/green]"
        if v == float("-inf"):
            return "[red]unlimited[/red]"
        return f"₹{v:+,.0f}"

    # Unknown lot: the money below is per unit and every label says so,
    # rather than multiplying by a number no source could give us.
    unit = "lot" if a.lot_size else "unit"
    table = Table(
        title=f"{a.symbol} {a.name} · spot {a.spot:,.2f} · "
              f"lot {a.lot_size or '—'} · exp {chain.expiry}",
        header_style="bold yellow", show_header=False,
    )
    table.add_column("k", style="dim")
    table.add_column("v")
    for leg in a.legs:
        table.add_row("Leg", leg.describe())
    prem = a.net_premium * (a.lot_size or 1)
    table.add_row("Net premium", f"{'credit' if prem >= 0 else 'debit'} ₹{abs(prem):,.0f}/{unit}")
    table.add_row("Max profit", money(a.max_profit))
    table.add_row("Max loss", money(a.max_loss))
    table.add_row("Breakevens", ", ".join(f"{b:,.0f}" for b in a.breakevens) or "none")
    table.add_row("POP (model)", f"{a.pop:.0%} (lognormal @ ATM IV)")
    for k in ("delta", "gamma", "theta", "vega"):
        table.add_row(f"{k} /{unit}", f"{a.greeks[k]:+,.2f}")
    console.print(table)
    console.print(f"[dim]Premiums from {chain.source}. Excludes margin, fees, slippage. Not advice.[/dim]")
    return 0


def cmd_iv(args) -> int:
    from shunkan.data.chains import get_chain
    from shunkan.derivatives import analyze_vol

    chain = get_chain(args.symbol)
    hist = _provider().history(args.symbol, period="1y", interval="1d")
    r = analyze_vol(chain, hist)
    table = Table(title=f"{r.symbol} volatility · exp {chain.expiry}",
                  header_style="bold yellow", show_header=False)
    table.add_column("k", style="dim")
    table.add_column("v", justify="right")
    table.add_row("Spot", f"{r.spot:,.2f}")
    table.add_row("ATM IV", f"{r.atm_iv:.1%}")
    table.add_row("Realized 21d (close-close)", f"{r.rv_cc_21:.1%}")
    table.add_row("Realized 21d (Parkinson)", f"{r.rv_park_21:.1%}")
    table.add_row("IV premium", f"{r.iv_premium * 100:+.1f} vol pts")
    table.add_row("RV percentile (1y)", f"{r.rv_percentile:.0%}")
    console.print(table)
    console.print("[bold yellow]Expected move (±1σ / ±2σ):[/bold yellow]")
    for days, (lo2, lo1, hi1, hi2) in r.cone.items():
        console.print(f"  {days:>2}d  [red]{lo2:,.0f}[/red]  {lo1:,.0f} ↔ {hi1:,.0f}  [green]{hi2:,.0f}[/green]")
    for note in r.notes:
        console.print(f"  · {note}")
    return 0


def cmd_montecarlo(args) -> int:
    from shunkan.backtest import BacktestConfig, get_strategy, monte_carlo, run_backtest

    strategy = get_strategy(args.strategy)
    hist = _provider().history(args.symbol, period=args.period, interval="1d")
    bt = run_backtest(hist, strategy.signal(hist), BacktestConfig(),
                      symbol=args.symbol, strategy_name=strategy.name)
    mc = monte_carlo(bt.returns, n_paths=args.paths)
    table = Table(
        title=f"Monte Carlo · {args.symbol.upper()} · {strategy.name} · {mc.n_paths:,} paths",
        header_style="bold yellow", show_header=False,
    )
    table.add_column("k", style="dim")
    table.add_column("v", justify="right")
    table.add_row("Terminal P5 / median / P95",
                  f"{mc.terminal_p5 - 1:+.1%} / {mc.terminal_p50 - 1:+.1%} / {mc.terminal_p95 - 1:+.1%}")
    table.add_row("P(loss)", f"{mc.prob_loss:.1%}")
    table.add_row("Max DD median", f"{mc.max_dd_median:.1%}")
    table.add_row("Max DD tail (worst 5%)", f"{mc.max_dd_p95:.1%}")
    table.add_row("Compute", f"{mc.elapsed_ms:.0f} ms")
    console.print(table)
    console.print(f"[bold]Verdict:[/bold] {mc.verdict()}")
    return 0


def cmd_screen(args) -> int:
    from shunkan.screener import UNIVERSES, run_screen

    universe = UNIVERSES.get(args.universe.lower())
    if universe is None:
        raise ValueError(f"Unknown universe '{args.universe}'. Choices: {', '.join(UNIVERSES)}")
    result = run_screen(_provider(), universe, args.rules, period=args.period)

    table = Table(
        title=f"Screen · {args.universe} · {' AND '.join(args.rules) or 'all'}",
        header_style="bold yellow",
    )
    for col in ("Symbol", "Price", "1W", "1M", "3M", "RSI", "Vol", "Off High", ">50d", ">200d"):
        table.add_column(col, justify="right")
    for sym, row in result.table.iterrows():
        table.add_row(
            f"[bold]{sym}[/bold]",
            f"{row['price']:,.2f}",
            _pct(row["ret_1w"]),
            _pct(row["ret_1mo"]),
            _pct(row["ret_3mo"]),
            f"{row['rsi']:.1f}",
            f"{row['vol_ann']:.0%}",
            _pct(row["from_high"]),
            "✓" if row["above_sma50"] else "·",
            "✓" if row["above_sma200"] else "·",
        )
    console.print(table)
    if result.errors:
        console.print(f"[dim]{len(result.errors)} symbols failed to fetch[/dim]")
    console.print(f"[dim]{len(result.table)}/{len(universe)} pass[/dim]")
    return 0


def cmd_pulse() -> int:
    from shunkan.intel import aggregate_bias, fetch_news
    from shunkan.markets import GLOBAL_PULSE, INDIA_PULSE, session_phase

    phase = session_phase()
    console.print(
        f"[bold yellow]NSE session:[/bold yellow] {phase.phase.replace('_', ' ')} — {phase.description}"
    )
    provider = _provider()
    for title, board in (("INDIA", INDIA_PULSE), ("GLOBAL", GLOBAL_PULSE)):
        table = Table(title=title, header_style="bold yellow")
        for col in ("Market", "Last", "Chg", "Chg%"):
            table.add_column(col, justify="right")
        try:
            quotes = provider.quotes([t for _, t in board])
        except Exception:
            quotes = {}
        for name, ticker in board:
            q = quotes.get(ticker.upper())
            if q is None:
                table.add_row(name, "n/a", "", "")
                continue
            color = "green" if q.change >= 0 else "red"
            table.add_row(
                f"[bold]{name}[/bold]", f"{q.price:,.2f}",
                f"[{color}]{q.change:+,.2f}[/{color}]", _pct(q.change_pct),
            )
        console.print(table)
    try:
        bias = aggregate_bias(fetch_news(limit=20))
        console.print(
            f"[bold]News bias:[/bold] {bias.label} (score {bias.score:+.2f}, "
            f"{bias.n_items} headlines)"
            + (f" · {bias.gap_call}" if bias.gap_call else "")
        )
    except Exception:
        console.print("[dim]News bias unavailable[/dim]")
    return 0


def cmd_chain(args) -> int:
    from shunkan.data.chains import get_chain
    from shunkan.derivatives.chain import analyze_chain

    chain = get_chain(args.symbol)
    a = analyze_chain(chain)
    atm = chain.atm_index
    half = max(args.strikes // 2, 3)
    lo, hi = max(atm - half, 0), min(atm + half + 1, len(chain.strikes))

    table = Table(
        title=f"{chain.symbol} option chain · spot {chain.spot:,.2f} · exp {chain.expiry} · {chain.source}",
        header_style="bold yellow",
    )
    for col in ("C-OI", "C-ΔOI", "C-IV", "C-LTP", "STRIKE", "P-LTP", "P-IV", "P-ΔOI", "P-OI"):
        table.add_column(col, justify="right")
    import numpy as np

    for i in range(lo, hi):
        mark = " ◀" if i == atm else ""
        iv_c = f"{chain.call_iv[i]:.1%}" if not np.isnan(chain.call_iv[i]) else "—"
        iv_p = f"{chain.put_iv[i]:.1%}" if not np.isnan(chain.put_iv[i]) else "—"
        table.add_row(
            f"{chain.call_oi[i]:,.0f}", f"{chain.call_oi_change[i]:+,.0f}", iv_c,
            f"{chain.call_ltp[i]:,.2f}",
            f"[bold yellow]{chain.strikes[i]:g}{mark}[/bold yellow]",
            f"{chain.put_ltp[i]:,.2f}", iv_p,
            f"{chain.put_oi_change[i]:+,.0f}", f"{chain.put_oi[i]:,.0f}",
        )
    console.print(table)
    console.print(
        f"PCR(OI) [bold]{a.pcr_oi:.2f}[/bold] · max pain [bold]{a.max_pain:g}[/bold] · "
        f"ATM IV [bold]{a.atm_iv:.1%}[/bold] · expected move [bold]±{a.expected_move_pct:.1%}[/bold] · "
        f"support {a.support:g} / resistance {a.resistance:g}"
    )
    console.print(f"[bold]Read:[/bold] {a.bias} — {a.bias_reason}")
    console.print("[dim]Heuristic positioning read, not trade advice.[/dim]")
    return 0


def cmd_vol(args) -> int:
    from shunkan.analytics.volume import analyze_volume

    hist = _provider().history(args.symbol, period=args.period, interval="1d")
    r = analyze_volume(hist)
    table = Table(title=f"Volume intelligence · {args.symbol.upper()} · {args.period}",
                  header_style="bold yellow", show_header=False)
    table.add_column("Metric", style="dim")
    table.add_column("Value")
    table.add_row("Day type", r.day_type)
    table.add_row("Volume z-score", f"{r.surge_z:+.2f}σ")
    table.add_row("Volume ratio", f"{r.surge_ratio:.2f}× 20-bar average")
    table.add_row("OBV signal", r.obv_divergence)
    table.add_row("POC", f"{r.profile.poc:,.0f}")
    table.add_row("Value area", f"{r.profile.value_area_low:,.0f} – {r.profile.value_area_high:,.0f}")
    console.print(table)
    for note in r.notes:
        console.print(f"  · {note}")
    return 0


def cmd_news(args) -> int:
    from shunkan.intel import aggregate_bias, assess_impact, summarize
    from shunkan.intel.feeds import fetch_news, symbol_news
    from shunkan.intel.sentiment import sentiment_label

    items = symbol_news(args.symbol, args.limit) if args.symbol else fetch_news(limit=args.limit)
    for item in items:
        call = assess_impact(item)
        s = sentiment_label(item.sentiment)
        color = "green" if "bullish" in s else "red" if "bearish" in s else "dim"
        console.print(f"[bold]{item.title}[/bold]")
        console.print(
            f"  [{color}]● {s} ({item.sentiment:+.2f})[/{color}] · {call.category.replace('_',' ')} "
            f"· {item.source} · {item.age_hours:.0f}h ago"
        )
        console.print(
            f"  impact: {call.direction} ({call.confidence:.0%}) · {call.magnitude} · {call.horizon}"
        )
        summary = summarize(item.description, max_sentences=1)
        if summary and summary.lower() not in item.title.lower():
            console.print(f"  [dim]{summary[:200]}[/dim]")
        console.print()
    bias = aggregate_bias(items)
    console.print(
        f"[bold yellow]Aggregate bias:[/bold yellow] {bias.label} (score {bias.score:+.2f})"
        + (f" · {bias.gap_call}" if bias.gap_call else "")
    )
    return 0


def cmd_connect(args) -> int:
    from shunkan.data.brokers import (
        CONNECT_HELP,
        GrowwProvider,
        KiteProvider,
        get_broker,
        kite_login_flow,
        load_credentials,
        save_credentials,
    )
    from shunkan.data.provider import DataError

    if args.broker == "zerodha":
        stored = load_credentials().get("zerodha", {})
        api_key = args.api_key or stored.get("api_key") or input("Kite api_key: ").strip()
        api_secret = (
            args.api_secret or stored.get("api_secret") or input("Kite api_secret: ").strip()
        )
        if not api_key or not api_secret:
            console.print("[red]Both api_key and api_secret are required.[/red]")
            return 1
        access_token = kite_login_flow(api_key, api_secret)
        console.print("[bold green]Zerodha connected — access token saved.[/bold green]")
        console.print("[dim]Tokens expire each morning; rerun `shunkan connect zerodha` daily.[/dim]")
        quote = KiteProvider(api_key, access_token).quote("RELIANCE")
        console.print(f"Live check: RELIANCE {quote.price:,.2f} ({quote.change_pct:+.2%})")
        return 0

    if args.broker == "groww":
        token = args.token or input("Groww api_token: ").strip()
        if not token:
            console.print("[red]An api_token is required.[/red]")
            return 1
        quote = GrowwProvider(token).quote("RELIANCE")  # validate before saving
        save_credentials("groww", api_token=token)
        console.print("[bold green]Groww connected — token saved.[/bold green]")
        console.print(f"Live check: RELIANCE {quote.price:,.2f} ({quote.change_pct:+.2%})")
        return 0

    try:
        broker = get_broker()
    except DataError:
        broker = None
    if broker:
        console.print(f"[bold green]Broker connected:[/bold green] {type(broker).__name__}")
    else:
        console.print("[bold yellow]No broker configured.[/bold yellow]")
    console.print(CONNECT_HELP)
    return 0


def cmd_strategies() -> int:
    from shunkan.backtest import STRATEGIES

    table = Table(title="Built-in strategies", header_style="bold yellow")
    table.add_column("Name", style="bold")
    table.add_column("Defaults")
    table.add_column("Description")
    for name, strat in sorted(STRATEGIES.items()):
        defaults = " ".join(f"{k}={v}" for k, v in strat.defaults.items()) or "—"
        table.add_row(name, defaults, strat.description)
    console.print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())

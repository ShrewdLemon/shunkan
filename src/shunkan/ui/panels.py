"""Content panels for the Shunkan terminal.

Each panel owns its data loading (threaded workers, so the UI never blocks)
and exposes a `show_*` entry point invoked by the app's command router.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import DataTable, Static
from textual_plotext import PlotextPlot

from shunkan.analytics import indicators as ta
from shunkan.backtest import BacktestConfig, get_strategy, grid_search, run_backtest
from shunkan.data.provider import DataError, Quote
from shunkan.portfolio import Portfolio
from shunkan.screener import UNIVERSES, run_screen

UP = "#3fb950"
DOWN = "#f85149"
DIM = "#8b949e"


def _pct_text(value: float, fmt: str = "{:+.2%}") -> Text:
    color = UP if value >= 0 else DOWN
    return Text(fmt.format(value), style=color)


def _num_text(value: float, fmt: str = "{:+,.2f}") -> Text:
    color = UP if value >= 0 else DOWN
    return Text(fmt.format(value), style=color)


class Panel(Container):
    """Base panel with a title bar and a status line."""

    TITLE = "Panel"

    def compose_header(self) -> ComposeResult:
        yield Static(self.TITLE, classes="panel-title", id=f"{self.id}-title")
        yield Static("", classes="panel-status", id=f"{self.id}-status")

    def set_status(self, message: str) -> None:
        try:
            self.query_one(f"#{self.id}-status", Static).update(message)
        except Exception:
            pass

    def set_title(self, message: str) -> None:
        try:
            self.query_one(f"#{self.id}-title", Static).update(message)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dashboard / watchlist
# ---------------------------------------------------------------------------


class DashboardPanel(Panel):
    TITLE = "WATCHLIST — quotes refresh every 30s. `w add SYM` / `w rm SYM` to edit."

    COLUMNS = ("Symbol", "Last", "Chg", "Chg%", "Day Range", "Volume", "Mkt Cap")

    def __init__(self, provider, watchlist: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self.watchlist = watchlist

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="watch-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#watch-table", DataTable)
        table.add_columns(*self.COLUMNS)
        self.refresh_quotes()
        self.set_interval(30.0, self.refresh_quotes)

    @work(thread=True, exclusive=True, group="dashboard")
    def refresh_quotes(self) -> None:
        if not self.watchlist:
            self.app.call_from_thread(self.set_status, "Watchlist empty — `w add AAPL`")
            return
        self.app.call_from_thread(self.set_status, "Refreshing quotes…")
        try:
            quotes = self.provider.quotes(self.watchlist)
        except DataError as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._fill, quotes)

    def _fill(self, quotes: dict[str, Quote]) -> None:
        table = self.query_one("#watch-table", DataTable)
        table.clear()
        for sym in self.watchlist:
            q = quotes.get(sym.upper())
            if q is None:
                table.add_row(sym, Text("n/a", style=DIM), "", "", "", "", "", key=sym)
                continue
            day_range = (
                f"{q.day_low:,.2f} – {q.day_high:,.2f}"
                if q.day_low is not None and q.day_high is not None
                else "—"
            )
            mcap = _fmt_compact(q.market_cap) if q.market_cap else "—"
            table.add_row(
                Text(q.symbol, style="bold"),
                f"{q.price:,.2f}",
                _num_text(q.change),
                _pct_text(q.change_pct),
                day_range,
                _fmt_compact(q.volume),
                mcap,
                key=sym,
            )
        self.set_status(f"{len(quotes)}/{len(self.watchlist)} quotes · updated just now")

    def set_watchlist(self, symbols: list[str]) -> None:
        self.watchlist = symbols
        self.refresh_quotes()


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------


class ChartPanel(Panel):
    TITLE = "CHART — `c SYM \\[period] \\[interval]` e.g. `c NVDA 1y 1d`"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield PlotextPlot(id="chart-plot")

    def show_chart(self, symbol: str, period: str = "6mo", interval: str = "1d") -> None:
        self.set_status(f"Loading {symbol} {period}/{interval}…")
        self._load(symbol, period, interval)

    @work(thread=True, exclusive=True, group="chart")
    def _load(self, symbol: str, period: str, interval: str) -> None:
        try:
            hist = self.provider.history(symbol, period=period, interval=interval)
        except DataError as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._draw_chart, symbol, period, interval, hist)

    # NB: do not name widget methods `_render`/`screen` — they shadow Textual internals.
    def _draw_chart(self, symbol: str, period: str, interval: str, hist: pd.DataFrame) -> None:
        plot = self.query_one("#chart-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")

        close = hist["close"]
        x = list(range(len(hist)))
        try:
            data = {
                "Open": hist["open"].tolist(),
                "Close": close.tolist(),
                "High": hist["high"].tolist(),
                "Low": hist["low"].tolist(),
            }
            plt.candlestick(x, data)
        except Exception:
            plt.plot(x, close.tolist(), label="close")

        for window, color in ((20, "yellow"), (50, "cyan")):
            if len(close) > window:
                ma = ta.sma(close, window)
                valid = ma.notna()
                plt.plot(
                    list(np.flatnonzero(valid.to_numpy())),
                    ma[valid].tolist(),
                    label=f"SMA{window}",
                    color=color,
                )

        ticks = np.linspace(0, len(hist) - 1, num=min(6, len(hist)), dtype=int)
        labels = [hist.index[i].strftime("%Y-%m-%d") for i in ticks]
        plt.xticks(ticks.tolist(), labels)

        last = float(close.iloc[-1])
        first = float(close.iloc[0])
        chg = last / first - 1.0
        plt.title(f"{symbol.upper()}  {period}  last {last:,.2f}  ({chg:+.2%} over period)")
        plot.refresh()
        self.set_status(
            f"{symbol.upper()} · {len(hist)} bars · {period}/{interval} · "
            f"range {float(hist['low'].min()):,.2f} – {float(hist['high'].max()):,.2f}"
        )


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


class BacktestPanel(Panel):
    TITLE = "BACKTEST — `bt SYM strategy \\[param=value …] \\[period]` · `opt SYM strategy \\[metric]`"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Horizontal(id="bt-layout"):
            yield PlotextPlot(id="bt-plot")
            yield VerticalScroll(Static(id="bt-metrics-body"), id="bt-metrics")

    def run_bt(self, symbol: str, strategy_name: str, params: dict, period: str) -> None:
        self.set_status(f"Backtesting {strategy_name} on {symbol} ({period})…")
        self._run_bt(symbol, strategy_name, params, period)

    def optimize(self, symbol: str, strategy_name: str, metric: str, period: str) -> None:
        self.set_status(f"Optimizing {strategy_name} on {symbol} by {metric}…")
        self._run_opt(symbol, strategy_name, metric, period)

    @work(thread=True, exclusive=True, group="backtest")
    def _run_bt(self, symbol: str, strategy_name: str, params: dict, period: str) -> None:
        try:
            strategy = get_strategy(strategy_name)
            hist = self.provider.history(symbol, period=period, interval="1d")
            signal = strategy.signal(hist, **params)
            result = run_backtest(
                hist, signal, BacktestConfig(), symbol=symbol,
                strategy_name=strategy.name, params={**strategy.defaults, **params},
            )
            bench = run_backtest(
                hist, get_strategy("buy_hold").signal(hist), BacktestConfig(),
                symbol=symbol, strategy_name="buy_hold",
            )
        except (DataError, KeyError, ValueError, TypeError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._render_bt, result, bench)

    def _render_bt(self, result, bench) -> None:
        plot = self.query_one("#bt-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")

        x = list(range(len(result.equity)))
        plt.plot(x, (result.equity / result.initial_cash).tolist(),
                 label=result.strategy_name, color="orange")
        plt.plot(x, (bench.equity / bench.initial_cash).tolist(),
                 label="buy & hold", color="cyan")
        ticks = np.linspace(0, len(x) - 1, num=min(6, len(x)), dtype=int)
        labels = [result.equity.index[i].strftime("%Y-%m") for i in ticks]
        plt.xticks(ticks.tolist(), labels)
        plt.title(f"{result.symbol.upper()} — growth of $1: {result.strategy_name} vs buy & hold")
        plot.refresh()

        lines = [f"[bold #ffb000]{result.symbol.upper()} · {result.strategy_name}[/]"]
        for label, value in result.summary_rows():
            lines.append(f"[#8b949e]{label:<18}[/] {value}")
        bench_ret = bench.total_return
        lines.append("")
        lines.append(f"[#8b949e]{'Buy & hold':<18}[/] {bench_ret:+.2%}")
        alpha = result.total_return - bench_ret
        color = "green" if alpha >= 0 else "red"
        lines.append(f"[#8b949e]{'Edge vs B&H':<18}[/] [{color}]{alpha:+.2%}[/]")
        self.query_one("#bt-metrics-body", Static).update("\n".join(lines))
        self.set_status(
            f"Done in {result.elapsed_ms:.1f} ms engine time · {len(result.trades)} trades"
        )

    def run_wf(self, symbol: str, strategy_name: str, metric: str, period: str) -> None:
        self.set_status(f"Walk-forward validating {strategy_name} on {symbol} ({period})…")
        self._run_wf(symbol, strategy_name, metric, period)

    @work(thread=True, exclusive=True, group="backtest")
    def _run_wf(self, symbol: str, strategy_name: str, metric: str, period: str) -> None:
        from shunkan.backtest import get_strategy as _get
        from shunkan.backtest.walkforward import walk_forward

        try:
            strategy = _get(strategy_name)
            hist = self.provider.history(symbol, period=period, interval="1d")
            wf = walk_forward(hist, strategy, metric=metric, symbol=symbol)
        except (DataError, KeyError, ValueError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._render_wf, wf)

    def _render_wf(self, wf) -> None:
        plot = self.query_one("#bt-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")
        if wf.oos_equity is not None and len(wf.oos_equity):
            x = list(range(len(wf.oos_equity)))
            plt.plot(x, (wf.oos_equity / wf.oos_equity.iloc[0]).tolist(),
                     label="out-of-sample equity", color="orange")
            ticks = np.linspace(0, len(x) - 1, num=min(5, len(x)), dtype=int)
            plt.xticks(ticks.tolist(),
                       [wf.oos_equity.index[i].strftime("%Y-%m") for i in ticks])
        plt.title(f"{wf.symbol.upper()} · {wf.strategy_name} — stitched OUT-OF-SAMPLE curve")
        plot.refresh()

        lines = [f"[bold #ffb000]Walk-forward · {wf.strategy_name}[/]"]
        lines.append(f"[#8b949e]{'Windows':<18}[/] {len(wf.windows)}")
        lines.append(f"[#8b949e]{'OOS return':<18}[/] {wf.oos_return:+.2%}")
        lines.append(f"[#8b949e]{'OOS Sharpe':<18}[/] {wf.oos_sharpe:.2f}")
        lines.append(f"[#8b949e]{'OOS max DD':<18}[/] {wf.oos_max_dd:.2%}")
        lines.append(f"[#8b949e]{'IS Sharpe (mean)':<18}[/] {wf.is_sharpe_mean:.2f}")
        lines.append(f"[#8b949e]{'Efficiency':<18}[/] {wf.efficiency:.0%}")
        lines.append(f"[#8b949e]{'Param stability':<18}[/] {wf.param_stability:.0%}")
        lines.append("")
        verdict_color = (
            "green" if "robust" in wf.verdict
            else "yellow" if "fragile" in wf.verdict else "red"
        )
        lines.append(f"[bold {verdict_color}]{wf.verdict}[/]")
        lines.append("")
        lines.append("[bold]Per window (IS→OOS Sharpe)[/]")
        for w in wf.windows:
            lines.append(
                f"  {str(w.test_start)[:10]}  {w.is_sharpe:+.2f} → {w.oos_sharpe:+.2f}  {w.best_params}"
            )
        self.query_one("#bt-metrics-body", Static).update("\n".join(lines))
        self.set_status(f"Walk-forward done · verdict: {wf.verdict}")

    def run_mc(self, symbol: str, strategy_name: str, params: dict, period: str) -> None:
        self.set_status(f"Monte Carlo: bootstrapping {strategy_name} on {symbol}…")
        self._run_mc(symbol, strategy_name, params, period)

    @work(thread=True, exclusive=True, group="backtest")
    def _run_mc(self, symbol: str, strategy_name: str, params: dict, period: str) -> None:
        from shunkan.backtest import monte_carlo

        try:
            strategy = get_strategy(strategy_name)
            hist = self.provider.history(symbol, period=period, interval="1d")
            bt = run_backtest(
                hist, strategy.signal(hist, **params), BacktestConfig(),
                symbol=symbol, strategy_name=strategy.name,
                params={**strategy.defaults, **params},
            )
            mc = monte_carlo(bt.returns)
        except (DataError, KeyError, ValueError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._render_mc, bt, mc)

    def _render_mc(self, bt, mc) -> None:
        plot = self.query_one("#bt-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")
        x = list(range(mc.n_bars))
        plt.plot(x, mc.envelope_p95.tolist(), label="P95", color="green")
        plt.plot(x, mc.envelope_p50.tolist(), label="median", color="orange")
        plt.plot(x, mc.envelope_p5.tolist(), label="P5", color="red")
        plt.plot(x, (bt.equity / bt.initial_cash).tolist(), label="actual", color="cyan")
        plt.title(
            f"{bt.symbol.upper()} · {bt.strategy_name} — {mc.n_paths:,} bootstrapped histories"
        )
        plot.refresh()

        lines = [f"[bold #ffb000]Monte Carlo · {bt.strategy_name}[/]"]
        lines.append(f"[#8b949e]{'Paths':<18}[/] {mc.n_paths:,} (block={mc.block_size})")
        lines.append(f"[#8b949e]{'Terminal P5':<18}[/] {mc.terminal_p5 - 1:+.1%}")
        lines.append(f"[#8b949e]{'Terminal median':<18}[/] {mc.terminal_p50 - 1:+.1%}")
        lines.append(f"[#8b949e]{'Terminal P95':<18}[/] {mc.terminal_p95 - 1:+.1%}")
        lines.append(f"[#8b949e]{'P(loss)':<18}[/] {mc.prob_loss:.1%}")
        lines.append(f"[#8b949e]{'Max DD median':<18}[/] {mc.max_dd_median:.1%}")
        lines.append(f"[#8b949e]{'Max DD tail (5%)':<18}[/] {mc.max_dd_p95:.1%}")
        lines.append(f"[#8b949e]{'Compute time':<18}[/] {mc.elapsed_ms:.0f} ms")
        lines.append("")
        verdict = mc.verdict()
        color = ("green" if "favorable" in verdict and "un" not in verdict
                 else "yellow" if "coin-flip" in verdict else "red")
        lines.append(f"[bold {color}]{verdict}[/]")
        self.query_one("#bt-metrics-body", Static).update("\n".join(lines))
        self.set_status(
            f"{mc.n_paths:,} paths in {mc.elapsed_ms:.0f} ms · P(loss) {mc.prob_loss:.0%}"
        )

    @work(thread=True, exclusive=True, group="backtest")
    def _run_opt(self, symbol: str, strategy_name: str, metric: str, period: str) -> None:
        try:
            strategy = get_strategy(strategy_name)
            hist = self.provider.history(symbol, period=period, interval="1d")
            opt = grid_search(hist, strategy, metric=metric, symbol=symbol)
        except (DataError, KeyError, ValueError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._render_opt, symbol, opt)

    def _render_opt(self, symbol: str, opt) -> None:
        plot = self.query_one("#bt-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")
        top = opt.table.head(15)
        labels = [
            " ".join(f"{k}={top.iloc[i][k]:g}" for k in opt.best_params)
            for i in range(len(top))
        ]
        plt.bar(labels, top[opt.metric].tolist(), orientation="horizontal", color="orange")
        plt.title(f"{symbol.upper()} · {opt.strategy_name} — top combos by {opt.metric}")
        plot.refresh()

        lines = [f"[bold #ffb000]Optimization · {opt.strategy_name}[/]"]
        lines.append(f"[#8b949e]{'Metric':<18}[/] {opt.metric}")
        lines.append(f"[#8b949e]{'Combos tested':<18}[/] {opt.combos_tested}")
        lines.append(f"[#8b949e]{'Wall time':<18}[/] {opt.elapsed_s*1000:.0f} ms")
        lines.append("")
        lines.append("[bold]Best parameters[/]")
        for k, v in opt.best_params.items():
            lines.append(f"[#8b949e]{k:<18}[/] {v:g}")
        if len(opt.table):
            best = opt.table.iloc[0]
            lines.append("")
            lines.append(f"[#8b949e]{'Sharpe':<18}[/] {best['sharpe']:.2f}")
            lines.append(f"[#8b949e]{'Total return':<18}[/] {best['total_return']:+.2%}")
            lines.append(f"[#8b949e]{'Max drawdown':<18}[/] {best['max_drawdown']:.2%}")
        self.query_one("#bt-metrics-body", Static).update("\n".join(lines))
        self.set_status(
            f"{opt.combos_tested} backtests in {opt.elapsed_s*1000:.0f} ms "
            f"({opt.combos_tested / max(opt.elapsed_s, 1e-9):,.0f} backtests/sec)"
        )


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------


class ScreenerPanel(Panel):
    TITLE = "SCREENER — `scr UNIVERSE rule \\[rule …]` e.g. `scr tech rsi<35 above_sma200`"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="screen-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#screen-table", DataTable)
        table.add_columns(
            "Symbol", "Price", "1W", "1M", "3M", "RSI", "Vol(ann)", "Off 6M High", "Vol Surge",
            ">SMA50", ">SMA200",
        )
        self.set_status(
            f"Universes: {', '.join(UNIVERSES)} · "
            "metrics: rsi, ret_1w/1mo/3mo, vol_ann, from_high, vol_surge, above_sma50/200"
        )

    # Named run_screener (not `screen`) to avoid shadowing Widget.screen.
    def run_screener(self, universe_name: str, rules: list[str]) -> None:
        self.set_status(f"Screening {universe_name} on {' AND '.join(rules) or 'no rules'}…")
        self._screen(universe_name, rules)

    @work(thread=True, exclusive=True, group="screener")
    def _screen(self, universe_name: str, rules: list[str]) -> None:
        universe = UNIVERSES.get(universe_name.lower())
        if universe is None:
            self.app.call_from_thread(
                self.set_status,
                f"[red]Unknown universe '{universe_name}'. Choices: {', '.join(UNIVERSES)}[/red]",
            )
            return
        try:
            result = run_screen(self.provider, universe, rules)
        except (ValueError, DataError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._fill, universe_name, result)

    def _fill(self, universe_name: str, result) -> None:
        table = self.query_one("#screen-table", DataTable)
        table.clear()
        for sym, row in result.table.iterrows():
            table.add_row(
                Text(str(sym), style="bold"),
                f"{row['price']:,.2f}",
                _pct_text(row["ret_1w"]) if pd.notna(row["ret_1w"]) else "—",
                _pct_text(row["ret_1mo"]) if pd.notna(row["ret_1mo"]) else "—",
                _pct_text(row["ret_3mo"]) if pd.notna(row["ret_3mo"]) else "—",
                f"{row['rsi']:.1f}" if pd.notna(row["rsi"]) else "—",
                f"{row['vol_ann']:.1%}" if pd.notna(row["vol_ann"]) else "—",
                _pct_text(row["from_high"]) if pd.notna(row["from_high"]) else "—",
                f"{row['vol_surge']:.2f}×" if pd.notna(row["vol_surge"]) else "—",
                "✓" if row["above_sma50"] else "·",
                "✓" if row["above_sma200"] else "·",
                key=str(sym),
            )
        msg = (
            f"{len(result.table)}/{len(result.universe)} pass "
            f"[{' AND '.join(result.rules) or 'no rules'}] in '{universe_name}'"
        )
        if result.errors:
            msg += f" · {len(result.errors)} fetch errors"
        self.set_status(msg)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


class PortfolioPanel(Panel):
    TITLE = "PORTFOLIO (paper) — `buy SYM QTY \\[price]` · `sell SYM QTY \\[price]` · `port`"

    def __init__(self, provider, portfolio: Portfolio, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self.portfolio = portfolio

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="port-table", zebra_stripes=True, cursor_type="row")
        yield Static("", id="port-summary", classes="panel-status")

    def on_mount(self) -> None:
        table = self.query_one("#port-table", DataTable)
        table.add_columns("Symbol", "Qty", "Avg Cost", "Last", "Mkt Value", "Unrlzd P&L", "Unrlzd %")
        self.refresh_positions()

    @work(thread=True, exclusive=True, group="portfolio")
    def refresh_positions(self) -> None:
        symbols = list(self.portfolio.positions)
        prices: dict[str, float] = {}
        if symbols:
            try:
                quotes = self.provider.quotes(symbols)
                prices = {s: q.price for s, q in quotes.items()}
            except DataError:
                prices = {}
        self.app.call_from_thread(self._fill, prices)

    def _fill(self, prices: dict[str, float]) -> None:
        table = self.query_one("#port-table", DataTable)
        table.clear()
        for sym, pos in sorted(self.portfolio.positions.items()):
            last = prices.get(sym, pos.avg_cost)
            mv = pos.market_value(last)
            upnl = pos.unrealized_pnl(last)
            upct = upnl / (pos.avg_cost * pos.quantity) if pos.avg_cost and pos.quantity else 0.0
            table.add_row(
                Text(sym, style="bold"),
                f"{pos.quantity:g}",
                f"{pos.avg_cost:,.2f}",
                f"{last:,.2f}",
                f"${mv:,.2f}",
                _num_text(upnl, "{:+,.2f}"),
                _pct_text(upct),
                key=sym,
            )
        equity = self.portfolio.total_equity(prices)
        self.query_one("#port-summary", Static).update(
            f"Cash ${self.portfolio.cash:,.2f} · Market value ${self.portfolio.market_value(prices):,.2f} · "
            f"Equity ${equity:,.2f} · Realized P&L {self.portfolio.realized_pnl:+,.2f} · "
            f"Unrealized {self.portfolio.unrealized_pnl(prices):+,.2f}"
        )
        self.set_status(f"{len(self.portfolio.positions)} positions · paper account")


# ---------------------------------------------------------------------------
# Quote snapshot
# ---------------------------------------------------------------------------


class QuotePanel(Panel):
    TITLE = "QUOTE — `q SYM` e.g. `q AAPL`"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield VerticalScroll(Static("Type `q SYM` to load a quote.", id="quote-body"))

    def show_quote(self, symbol: str) -> None:
        self.set_status(f"Loading {symbol}…")
        self._load(symbol)

    @work(thread=True, exclusive=True, group="quote")
    def _load(self, symbol: str) -> None:
        try:
            quote = self.provider.quote(symbol)
            hist = self.provider.history(symbol, period="1y", interval="1d")
        except DataError as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._fill, quote, hist)

    def _fill(self, q: Quote, hist: pd.DataFrame) -> None:
        close = hist["close"]
        high_52w = float(close.max())
        low_52w = float(close.min())
        rsi_val = ta.rsi(close, 14).dropna()
        sma50 = ta.sma(close, 50).dropna()
        sma200 = ta.sma(close, 200).dropna()
        ret_1y = float(close.iloc[-1] / close.iloc[0] - 1.0)
        vol_ann = float(close.pct_change().std(ddof=1) * np.sqrt(252))

        chg_color = UP if q.change >= 0 else DOWN
        arrow = "▲" if q.change >= 0 else "▼"
        lines = [
            f"[bold]{q.symbol}[/bold]" + (f"  [#8b949e]{q.name}[/]" if q.name else ""),
            "",
            f"[bold {chg_color}]{q.price:,.2f}  {arrow} {q.change:+,.2f} ({q.change_pct:+.2%})[/]",
            "",
            f"[#8b949e]{'Prev close':<16}[/] {q.prev_close:,.2f}",
            f"[#8b949e]{'Day range':<16}[/] "
            + (f"{q.day_low:,.2f} – {q.day_high:,.2f}" if q.day_low is not None else "—"),
            f"[#8b949e]{'52w range':<16}[/] {low_52w:,.2f} – {high_52w:,.2f}",
            f"[#8b949e]{'Off 52w high':<16}[/] {q.price / high_52w - 1.0:+.2%}",
            f"[#8b949e]{'1y return':<16}[/] {ret_1y:+.2%}",
            f"[#8b949e]{'Volatility(ann)':<16}[/] {vol_ann:.1%}",
            f"[#8b949e]{'Volume':<16}[/] {_fmt_compact(q.volume)}",
            f"[#8b949e]{'Market cap':<16}[/] {_fmt_compact(q.market_cap)}",
            "",
            f"[#8b949e]{'RSI(14)':<16}[/] " + (f"{float(rsi_val.iloc[-1]):.1f}" if len(rsi_val) else "—"),
            f"[#8b949e]{'vs SMA50':<16}[/] "
            + (f"{q.price / float(sma50.iloc[-1]) - 1.0:+.2%}" if len(sma50) else "—"),
            f"[#8b949e]{'vs SMA200':<16}[/] "
            + (f"{q.price / float(sma200.iloc[-1]) - 1.0:+.2%}" if len(sma200) else "—"),
        ]
        self.query_one("#quote-body", Static).update("\n".join(lines))
        self.set_status(f"{q.symbol} · quote + 1y technicals")


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


class NewsPanel(Panel):
    TITLE = "NEWS INTEL — `news` (market) · `n RELIANCE` — sentiment, summary, impact call"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield Static("", id="news-bias", classes="panel-status")
        yield VerticalScroll(id="news-scroll")

    def show_news(self, symbol: str | None = None) -> None:
        self.set_status(f"Loading {'market' if not symbol else symbol} news + scoring impact…")
        self._load(symbol)

    @work(thread=True, exclusive=True, group="news")
    def _load(self, symbol: str | None) -> None:
        from shunkan.intel import aggregate_bias, assess_impact, summarize
        from shunkan.intel.feeds import fetch_news, symbol_news

        try:
            items = symbol_news(symbol, limit=15) if symbol else fetch_news(limit=20)
        except Exception as exc:
            self.app.call_from_thread(
                self.set_status, f"[red]News fetch failed: {exc}[/red]"
            )
            return
        calls = []
        for item in items:
            call = assess_impact(item)
            item.summary = summarize(item.description or item.title, max_sentences=1)
            calls.append(call)
        bias = aggregate_bias(items)
        self.app.call_from_thread(self._fill, symbol, items, calls, bias)

    def _fill(self, symbol, items, calls, bias) -> None:
        from shunkan.intel.sentiment import sentiment_label

        bias_color = UP if "bullish" in bias.label else DOWN if "bearish" in bias.label else DIM
        gap = f" · [italic]{bias.gap_call}[/italic]" if bias.gap_call else ""
        self.query_one("#news-bias", Static).update(
            f"[bold]Aggregate bias:[/bold] [{bias_color}]{bias.label}[/] "
            f"(score {bias.score:+.2f}, {bias.n_items} headlines, recency-weighted){gap}"
        )

        scroll = self.query_one("#news-scroll", VerticalScroll)
        scroll.remove_children()
        if not items:
            scroll.mount(Static("No headlines found."))
        for item, call in zip(items, calls):
            s_label = sentiment_label(item.sentiment)
            s_color = UP if "bullish" in s_label else DOWN if "bearish" in s_label else DIM
            age = f"{item.age_hours:.0f}h ago" if item.age_hours < 900 else ""
            body = (
                f"[bold]{item.title}[/bold]\n"
                f"[{s_color}]● {s_label} ({item.sentiment:+.2f})[/] "
                f"[#8b949e]· {call.category.replace('_', ' ')} · {item.source} · {age}[/]\n"
                f"[#8b949e]impact:[/] {call.direction} ({call.confidence:.0%} conf) · "
                f"{call.magnitude} · {call.horizon} · hits: {call.segment}"
            )
            if item.summary and item.summary.lower() not in item.title.lower():
                body += f"\n[dim]{item.summary[:220]}[/dim]"
            scroll.mount(Static(body, classes="news-item"))
        target = symbol or "Indian markets"
        self.set_status(
            f"{len(items)} stories · {target} · sentiment+impact scored locally in <1ms/headline"
        )


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


HELP_TEXT = r"""[bold #ffb000]SHUNKAN 瞬間 — command reference (India-first)[/]

Type a symbol the way you say it: RELIANCE, NIFTY, BANKNIFTY, SENSEX, VIX.
Shunkan resolves the rest. US tickers (AAPL, ^GSPC) still work.

[bold]The big four[/]
  pulse                     India + global market board with news bias (home)
  oc NIFTY                  Option chain: OI walls, PCR, max pain, IV, greeks,
                            expected move, unusual activity, buildup read
  news  /  n RELIANCE       Headlines with sentiment, AI summary, and a
                            timing-aware impact call (gap risk, horizon)
  vol RELIANCE              Volume intelligence: profile/POC, surges, OBV flow

[bold]Derivatives lab[/]
  pay NIFTY iron_condor     Strategy payoff: breakevens, max P/L, POP,
                            position greeks. Templates: straddle, strangle,
                            (short_ variants), bull_call_spread,
                            bear_put_spread, iron_condor, iron_fly
  pay NIFTY +23200CE -23400CE   Custom legs at live chain premiums
  iv NIFTY                  IV smile, IV vs realized vol, expected-move cone
  mc NIFTY sma_cross 5y     Monte Carlo: 2,000 bootstrapped histories,
                            P(loss), drawdown distribution

[bold]Market data[/]
  q NIFTY                   Quote snapshot + 1y technicals
  c BANKNIFTY 6mo 1d        Candlestick chart with SMA20/50
                            periods: 1mo 3mo 6mo 1y 2y 5y 10y max

[bold]Watchlist[/]
  w                         Watchlist dashboard (auto-refresh 30s)
  w add SYM … / w rm SYM …  Edit watchlist

[bold]Live tape & alerts[/]
  tape                      Streaming ticks (Kite WebSocket when connected,
                            demo random-walk feed otherwise)
  alert NIFTY > 23500       Price alert (checked every 60s, fires once,
                            in-app + macOS notification)
  alert RELIANCE rsi < 30   RSI(14) alert · also: vol_surge (vs 20-day avg)
  alerts / alert rm N       List / remove alerts

[bold]Backtesting (vectorized — milliseconds)[/]
  bt NIFTY sma_cross fast=10 slow=50 5y    Backtest vs buy & hold
  opt NIFTY ema_cross sharpe               Grid-search parameters
  wf NIFTY sma_cross 10y                   Walk-forward validation: optimize
                                           in-sample, verify out-of-sample,
                                           verdict robust/fragile/overfit
  strategies                               List built-in strategies

[bold]Screening[/]
  scr nifty50 rsi<35 above_sma200          AND-ed rules over a universe
       universes: nifty50 banks it fno mega tech semis etf
       metrics: rsi ret_1w ret_1mo ret_3mo vol_ann from_high vol_surge
                above_sma50 above_sma200

[bold]Paper portfolio[/]
  port / buy SYM QTY / sell SYM QTY        Paper trades, FIFO P&L

[bold]Brokers[/]
  connect                   Zerodha Kite / Groww API setup for real-time data

[bold]Keys[/] F1 help · F2 pulse · F3 chart · F4 backtest · F5 chain · F6 volume
      F7 news · F8 portfolio · F9 screener · F10 watchlist
"""


class HelpPanel(Panel):
    TITLE = "HELP"

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield VerticalScroll(Static(HELP_TEXT, id="help-body"))

    def show_text(self, text: str) -> None:
        """Temporarily display other reference text (e.g. broker setup)."""
        self.query_one("#help-body", Static).update(text)

    def show_help(self) -> None:
        self.query_one("#help-body", Static).update(HELP_TEXT)


def _fmt_compact(value: float | int | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= threshold:
            return f"{v / threshold:,.2f}{suffix}"
    return f"{v:,.0f}"

"""The Shunkan terminal application.

A Bloomberg-style command bar drives everything: type `help` for the full
reference. F-keys jump straight to panels.
"""

from __future__ import annotations

import os

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import ContentSwitcher, Footer, Header, Input

from shunkan import __version__
from shunkan.alerts import AlertBook, desktop_notify, parse_alert
from shunkan.backtest import STRATEGIES
from shunkan.config import load_watchlist, save_watchlist
from shunkan.data.provider import DataError, get_provider
from shunkan.markets import session_phase
from shunkan.portfolio import Portfolio
from shunkan.ui.panels import (
    BacktestPanel,
    ChartPanel,
    DashboardPanel,
    HelpPanel,
    NewsPanel,
    PortfolioPanel,
    QuotePanel,
    ScreenerPanel,
)
from shunkan.ui.panels_india import (
    IVPanel,
    OptionChainPanel,
    PayoffPanel,
    PulsePanel,
    TapePanel,
    VolumePanel,
)

PERIODS = {"1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max", "ytd", "5d"}
INTERVALS = {"1m", "2m", "5m", "15m", "30m", "1h", "1d", "5d", "1wk", "1mo"}


class ShunkanApp(App):
    CSS_PATH = "shunkan.tcss"
    TITLE = "SHUNKAN 瞬間"

    BINDINGS = [
        ("f1", "switch_panel('help')", "Help"),
        ("f2", "switch_panel('pulse')", "Pulse"),
        ("f3", "switch_panel('chart')", "Chart"),
        ("f4", "switch_panel('backtest')", "Backtest"),
        ("f5", "switch_panel('chain')", "Chain"),
        ("f6", "switch_panel('volume')", "Volume"),
        ("f7", "switch_panel('news')", "News"),
        ("f8", "switch_panel('portfolio')", "Portfolio"),
        ("f9", "switch_panel('screener')", "Screener"),
        ("f10", "switch_panel('dashboard')", "Watchlist"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, provider=None, portfolio: Portfolio | None = None) -> None:
        super().__init__()
        self.provider = provider or get_provider()
        self.watchlist = load_watchlist()
        self.portfolio = portfolio or Portfolio.load()
        self.alert_book = AlertBook()
        offline = os.environ.get("SHUNKAN_OFFLINE", "").strip() in {"1", "true", "yes"}
        phase = session_phase()
        self.sub_title = (
            f"v{__version__} · {'OFFLINE' if offline else 'LIVE'} · "
            f"NSE {phase.phase.replace('_', ' ')}"
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with ContentSwitcher(initial="pulse", id="main-switcher"):
            yield PulsePanel(self.provider, id="pulse", classes="panel")
            yield DashboardPanel(self.provider, self.watchlist, id="dashboard", classes="panel")
            yield QuotePanel(self.provider, id="quote", classes="panel")
            yield ChartPanel(self.provider, id="chart", classes="panel")
            yield OptionChainPanel(id="chain", classes="panel")
            yield VolumePanel(self.provider, id="volume", classes="panel")
            self._tape = TapePanel(self.watchlist, id="tape", classes="panel")
            yield self._tape
            yield PayoffPanel(id="payoff", classes="panel")
            yield IVPanel(self.provider, id="iv", classes="panel")
            yield BacktestPanel(self.provider, id="backtest", classes="panel")
            yield ScreenerPanel(self.provider, id="screener", classes="panel")
            yield PortfolioPanel(self.provider, self.portfolio, id="portfolio", classes="panel")
            yield NewsPanel(self.provider, id="news", classes="panel")
            yield HelpPanel(id="help", classes="panel")
        with Container(id="command-bar"):
            yield Input(
                placeholder="oc NIFTY · news · vol RELIANCE · c BANKNIFTY · bt NIFTY sma_cross · help",
                id="command-input",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#command-input", Input).focus()
        self.set_interval(60.0, self._check_alerts)

    def on_unmount(self) -> None:
        # DOM may already be gone during shutdown — use the direct reference.
        tape = getattr(self, "_tape", None)
        if tape is not None:
            tape.stop_stream()

    @work(thread=True, exclusive=True, group="alerts")
    def _check_alerts(self) -> None:
        if not self.alert_book.armed:
            return
        fired = self.alert_book.check_all(self.provider)
        for alert, current in fired:
            metric = "" if alert.metric == "price" else f" {alert.metric}"
            msg = f"⚡ {alert.symbol}{metric} {alert.op} {alert.value:g} — now {current:,.2f}"
            self.call_from_thread(self.notify, msg, title="Shunkan alert", timeout=20)
            desktop_notify("Shunkan alert", msg)

    # -- command routing ----------------------------------------------------

    def action_switch_panel(self, panel_id: str) -> None:
        self.query_one("#main-switcher", ContentSwitcher).current = panel_id

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-input":
            return
        line = event.value.strip()
        event.input.value = ""
        if not line:
            return
        try:
            self.dispatch(line)
        except (ValueError, KeyError, DataError) as exc:
            self.notify(str(exc), severity="error", timeout=6)

    def dispatch(self, line: str) -> None:
        tokens = line.split()
        cmd, args = tokens[0].lower(), tokens[1:]

        if cmd in {"quit", "exit", ":q"}:
            self.portfolio.save()
            self.exit()
        elif cmd in {"help", "?", "h"}:
            self.query_one(HelpPanel).show_help()
            self.action_switch_panel("help")
        elif cmd in {"pulse", "p", "home"}:
            self.action_switch_panel("pulse")
            self.query_one(PulsePanel).refresh_pulse()
        elif cmd in {"oc", "chain"}:
            sym = self._need_symbol(args, "oc NIFTY")
            self.action_switch_panel("chain")
            self.query_one(OptionChainPanel).show_chain(sym)
        elif cmd in {"vol", "volume"}:
            sym = self._need_symbol(args, "vol RELIANCE")
            period = args[1] if len(args) > 1 and args[1] in PERIODS else "6mo"
            self.action_switch_panel("volume")
            self.query_one(VolumePanel).show_volume(sym, period)
        elif cmd in {"pay", "payoff", "strat"}:
            if len(args) < 2:
                raise ValueError(
                    "Usage: pay SYM STRATEGY|LEGS — e.g. `pay NIFTY iron_condor` "
                    "or `pay NIFTY +23200CE -23400CE`"
                )
            sym, spec = args[0].upper(), args[1:]
            width = 2
            if spec and spec[-1].isdigit():
                width = int(spec[-1])
                spec = spec[:-1]
            self.action_switch_panel("payoff")
            self.query_one(PayoffPanel).show_payoff(sym, spec, width)
        elif cmd == "iv":
            sym = self._need_symbol(args, "iv NIFTY")
            self.action_switch_panel("iv")
            self.query_one(IVPanel).show_iv(sym)
        elif cmd in {"mc", "montecarlo"}:
            sym, strat, params, period, extras = self._parse_bt_args(args, "5y")
            if extras:
                raise ValueError(f"Unrecognized arguments: {' '.join(extras)}")
            self.action_switch_panel("backtest")
            self.query_one(BacktestPanel).run_mc(sym, strat, params, period)
        elif cmd in {"tape", "live"}:
            self.action_switch_panel("tape")
            self.query_one(TapePanel).start_stream()
        elif cmd == "alerts":
            self._show_alerts()
        elif cmd == "alert":
            self._handle_alert(args)
        elif cmd == "connect":
            from shunkan.data.brokers import CONNECT_HELP, get_broker

            broker = None
            try:
                broker = get_broker()
            except DataError:
                pass
            status = (
                f"[bold green]Broker connected: {type(broker).__name__}[/]\n\n"
                if broker else "[bold yellow]No broker configured yet.[/]\n\n"
            )
            self.query_one(HelpPanel).show_text(status + CONNECT_HELP)
            self.action_switch_panel("help")
        elif cmd in {"q", "quote"}:
            sym = self._need_symbol(args, "q AAPL")
            self.action_switch_panel("quote")
            self.query_one(QuotePanel).show_quote(sym)
        elif cmd in {"c", "chart"}:
            sym = self._need_symbol(args, "c AAPL 6mo 1d")
            period = args[1] if len(args) > 1 else "6mo"
            interval = args[2] if len(args) > 2 else "1d"
            if period not in PERIODS:
                raise ValueError(f"Unknown period '{period}'. Choices: {', '.join(sorted(PERIODS))}")
            if interval not in INTERVALS:
                raise ValueError(f"Unknown interval '{interval}'. Choices: {', '.join(sorted(INTERVALS))}")
            self.action_switch_panel("chart")
            self.query_one(ChartPanel).show_chart(sym, period, interval)
        elif cmd in {"n", "news"}:
            # `news` with no symbol = whole-market feed with aggregate bias.
            sym = args[0].upper() if args else None
            self.action_switch_panel("news")
            self.query_one(NewsPanel).show_news(sym)
        elif cmd in {"w", "watch", "watchlist"}:
            self._handle_watch(args)
        elif cmd == "bt":
            self._handle_backtest(args)
        elif cmd in {"opt", "optimize"}:
            self._handle_optimize(args)
        elif cmd in {"wf", "walkforward"}:
            if len(args) < 2:
                raise ValueError("Usage: wf SYM STRATEGY [metric] [period]")
            sym, strat = args[0].upper(), args[1].lower()
            metric, period = "sharpe", "10y"
            for token in args[2:]:
                if token in PERIODS:
                    period = token
                else:
                    metric = token
            self.action_switch_panel("backtest")
            self.query_one(BacktestPanel).run_wf(sym, strat, metric, period)
        elif cmd == "strategies":
            self._show_strategies()
        elif cmd in {"scr", "screen", "screener"}:
            if not args:
                self.action_switch_panel("screener")
                return
            universe, rules = args[0], args[1:]
            self.action_switch_panel("screener")
            self.query_one(ScreenerPanel).run_screener(universe, rules)
        elif cmd in {"port", "portfolio", "pos"}:
            self.action_switch_panel("portfolio")
            self.query_one(PortfolioPanel).refresh_positions()
        elif cmd in {"buy", "sell"}:
            self._handle_trade(cmd, args)
        else:
            raise ValueError(f"Unknown command '{cmd}'. Type `help` for the reference.")

    # -- handlers -------------------------------------------------------------

    def _need_symbol(self, args: list[str], example: str) -> str:
        if not args:
            raise ValueError(f"Symbol required, e.g. `{example}`")
        return args[0].upper()

    def _handle_watch(self, args: list[str]) -> None:
        dashboard = self.query_one(DashboardPanel)
        if args and args[0].lower() in {"add", "rm", "remove", "del"}:
            action, symbols = args[0].lower(), [s.upper() for s in args[1:]]
            if not symbols:
                raise ValueError("Give at least one symbol, e.g. `w add TSLA`")
            if action == "add":
                self.watchlist = sorted(set(self.watchlist) | set(symbols))
            else:
                self.watchlist = [s for s in self.watchlist if s not in set(symbols)]
            save_watchlist(self.watchlist)
            dashboard.set_watchlist(self.watchlist)
            self.notify(f"Watchlist now {len(self.watchlist)} symbols")
        self.action_switch_panel("dashboard")

    def _parse_bt_args(self, args: list[str], default_period: str) -> tuple[str, str, dict, str, list[str]]:
        if len(args) < 2:
            raise ValueError("Usage: bt SYM STRATEGY [param=value …] [period]")
        sym, strat = args[0].upper(), args[1].lower()
        params: dict[str, float] = {}
        period = default_period
        extras: list[str] = []
        for token in args[2:]:
            if "=" in token:
                key, _, raw = token.partition("=")
                try:
                    value = float(raw)
                except ValueError as exc:
                    raise ValueError(f"Bad parameter '{token}': value must be numeric") from exc
                params[key] = int(value) if value == int(value) and "." not in raw else value
            elif token in PERIODS:
                period = token
            else:
                extras.append(token)
        return sym, strat, params, period, extras

    def _handle_backtest(self, args: list[str]) -> None:
        sym, strat, params, period, extras = self._parse_bt_args(args, "5y")
        if extras:
            raise ValueError(f"Unrecognized backtest arguments: {' '.join(extras)}")
        self.action_switch_panel("backtest")
        self.query_one(BacktestPanel).run_bt(sym, strat, params, period)

    def _handle_optimize(self, args: list[str]) -> None:
        if len(args) < 2:
            raise ValueError("Usage: opt SYM STRATEGY [metric] [period]")
        sym, strat = args[0].upper(), args[1].lower()
        metric, period = "sharpe", "5y"
        for token in args[2:]:
            if token in PERIODS:
                period = token
            else:
                metric = token
        self.action_switch_panel("backtest")
        self.query_one(BacktestPanel).optimize(sym, strat, metric, period)

    def _show_strategies(self) -> None:
        from textual.widgets import Static

        lines = ["[bold #ffb000]Strategies[/]"]
        for name, strat in sorted(STRATEGIES.items()):
            defaults = " ".join(f"{k}={v}" for k, v in strat.defaults.items())
            lines.append(f"[bold]{name}[/] [#8b949e]{defaults}[/]")
            lines.append(f"  {strat.description}")
        self.action_switch_panel("backtest")
        self.query_one("#bt-metrics-body", Static).update("\n".join(lines))

    def _handle_alert(self, args: list[str]) -> None:
        if not args:
            self._show_alerts()
            return
        if args[0].lower() in {"rm", "remove", "del"}:
            if len(args) < 2 or not args[1].isdigit():
                raise ValueError("Usage: alert rm N (see `alerts` for numbers)")
            gone = self.alert_book.remove(int(args[1]) - 1)
            self.notify(f"Removed alert: {gone.describe()}")
            return
        alert = parse_alert(" ".join(args))
        self.alert_book.add(alert)
        self.notify(f"Alert armed: {alert.describe()} (checked every 60s)")

    def _show_alerts(self) -> None:
        from textual.widgets import Static

        lines = ["[bold #ffb000]Alerts[/] (checked every 60s while Shunkan runs)"]
        if not self.alert_book.alerts:
            lines.append("none — try `alert NIFTY > 23500` or `alert RELIANCE rsi < 30`")
        for i, alert in enumerate(self.alert_book.alerts, 1):
            lines.append(f"{i}. {alert.describe()}")
        lines.append("")
        lines.append("`alert rm N` removes one.")
        self.query_one(HelpPanel).show_text("\n".join(lines))
        self.action_switch_panel("help")

    def _handle_trade(self, side: str, args: list[str]) -> None:
        if len(args) < 2:
            raise ValueError(f"Usage: {side} SYM QTY [price]")
        sym = args[0].upper()
        try:
            qty = float(args[1])
        except ValueError as exc:
            raise ValueError(f"Quantity must be a number, got '{args[1]}'") from exc
        price = float(args[2]) if len(args) > 2 else None
        self._execute_trade(side, sym, qty, price)

    @work(thread=True, exclusive=True, group="trade")
    def _execute_trade(self, side: str, sym: str, qty: float, price: float | None) -> None:
        try:
            if price is None:
                price = self.provider.quote(sym).price
            if side == "buy":
                self.portfolio.buy(sym, qty, price)
            else:
                realized = self.portfolio.sell(sym, qty, price)
            self.portfolio.save()
        except (ValueError, DataError) as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=6)
            return
        if side == "buy":
            msg = f"Paper BUY {qty:g} {sym} @ {price:,.2f}"
        else:
            msg = f"Paper SELL {qty:g} {sym} @ {price:,.2f} · realized {realized:+,.2f}"
        self.call_from_thread(self.notify, msg)
        self.call_from_thread(self.action_switch_panel, "portfolio")
        self.call_from_thread(self.query_one(PortfolioPanel).refresh_positions)


def run() -> None:
    ShunkanApp().run()


if __name__ == "__main__":
    run()

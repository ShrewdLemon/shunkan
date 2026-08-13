"""India-focused panels: market pulse, option chain, volume intelligence."""

from __future__ import annotations

import numpy as np
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Static
from textual_plotext import PlotextPlot

from shunkan.analytics.volume import analyze_volume
from shunkan.data.chains import get_chain
from shunkan.data.provider import DataError
from shunkan.derivatives.chain import analyze_chain, classify_buildup
from shunkan.intel import aggregate_bias, fetch_news
from shunkan.markets import GLOBAL_PULSE, INDIA_PULSE, session_phase
from shunkan.ui.panels import DIM, DOWN, UP, Panel, _fmt_compact, _num_text, _pct_text

GOLD = "#ffb000"


# ---------------------------------------------------------------------------
# Market pulse — the home screen
# ---------------------------------------------------------------------------


class PulsePanel(Panel):
    TITLE = "MARKET PULSE — India + global at a glance · refreshes every 30s"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="pulse-table", zebra_stripes=True, cursor_type="row")
        yield Static("", id="pulse-bias", classes="panel-status")

    def on_mount(self) -> None:
        table = self.query_one("#pulse-table", DataTable)
        table.add_columns("Market", "Last", "Chg", "Chg%", "Session note")
        self.refresh_pulse()
        self.set_interval(30.0, self.refresh_pulse)

    @work(thread=True, exclusive=True, group="pulse")
    def refresh_pulse(self) -> None:
        phase = session_phase()
        self.app.call_from_thread(
            self.set_status,
            f"IST session: {phase.phase.replace('_', ' ')} — {phase.description}",
        )
        rows: list[tuple] = []
        boards = [("— INDIA —", INDIA_PULSE), ("— GLOBAL —", GLOBAL_PULSE)]
        for header, board in boards:
            rows.append((header,))
            try:
                quotes = self.provider.quotes([t for _, t in board])
            except DataError:
                quotes = {}
            for name, ticker in board:
                q = quotes.get(ticker.upper())
                rows.append((name, q))
        self.app.call_from_thread(self._fill, rows)

        # News bias is slower — fetch after quotes are painted.
        try:
            items = fetch_news(limit=20)
            bias = aggregate_bias(items)
            line = (
                f"[bold]News bias:[/bold] {_bias_markup(bias.label)} "
                f"(score {bias.score:+.2f} over {bias.n_items} headlines)"
            )
            if bias.gap_call:
                line += f" · [italic]{bias.gap_call}[/italic]"
            self.app.call_from_thread(
                self.query_one("#pulse-bias", Static).update, line
            )
        except Exception:
            self.app.call_from_thread(
                self.query_one("#pulse-bias", Static).update,
                "[dim]News bias unavailable (offline?)[/dim]",
            )

    def _fill(self, rows: list[tuple]) -> None:
        table = self.query_one("#pulse-table", DataTable)
        table.clear()
        vix_note = {"INDIA VIX": "fear gauge — >18 is jumpy, >24 is stressed"}
        for row in rows:
            if len(row) == 1:
                table.add_row(Text(row[0], style=f"bold {GOLD}"), "", "", "", "")
                continue
            name, q = row
            if q is None:
                table.add_row(name, Text("n/a", style=DIM), "", "", "")
                continue
            note = vix_note.get(name, "")
            table.add_row(
                Text(name, style="bold"),
                f"{q.price:,.2f}",
                _num_text(q.change),
                _pct_text(q.change_pct),
                Text(note, style=DIM),
            )


def _bias_markup(label: str) -> str:
    if "bullish" in label:
        return f"[{UP}]{label}[/]"
    if "bearish" in label:
        return f"[{DOWN}]{label}[/]"
    return f"[{DIM}]{label}[/]"


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------


class OptionChainPanel(Panel):
    TITLE = "OPTION CHAIN — `oc NIFTY` · `oc BANKNIFTY` · `oc RELIANCE`"

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="chain-table", zebra_stripes=False, cursor_type="row")
        yield VerticalScroll(Static("", id="chain-analytics"), id="chain-scroll")

    def on_mount(self) -> None:
        table = self.query_one("#chain-table", DataTable)
        table.add_columns(
            "C·OI", "C·ΔOI", "C·Vol", "C·IV", "C·LTP",
            "STRIKE",
            "P·LTP", "P·IV", "P·Vol", "P·ΔOI", "P·OI",
        )
        self.query_one("#chain-scroll").styles.height = 12

    def show_chain(self, symbol: str) -> None:
        self.set_status(f"Loading option chain for {symbol}…")
        self._load(symbol)

    @work(thread=True, exclusive=True, group="chain")
    def _load(self, symbol: str) -> None:
        try:
            chain = get_chain(symbol)
            analytics = analyze_chain(chain)
        except (DataError, ValueError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._fill, chain, analytics)

    def _fill(self, chain, a) -> None:
        table = self.query_one("#chain-table", DataTable)
        table.clear()
        atm_i = chain.atm_index
        max_c_oi = float(chain.call_oi.max()) or 1.0
        max_p_oi = float(chain.put_oi.max()) or 1.0

        for i, strike in enumerate(chain.strikes):
            is_atm = i == atm_i
            c_wall = chain.call_oi[i] >= 0.999 * max_c_oi
            p_wall = chain.put_oi[i] >= 0.999 * max_p_oi
            strike_style = f"bold {GOLD}" if is_atm else "bold"
            strike_label = f"{strike:g}" + (" ◀" if is_atm else "")

            def iv_str(v):
                return f"{v:.1%}" if not np.isnan(v) else "—"

            table.add_row(
                Text(_fmt_compact(chain.call_oi[i]) + ("⛔" if c_wall else ""),
                     style=DOWN if c_wall else ""),
                _num_text(chain.call_oi_change[i], "{:+,.0f}"),
                _fmt_compact(chain.call_volume[i]),
                iv_str(chain.call_iv[i]),
                f"{chain.call_ltp[i]:,.2f}",
                Text(strike_label, style=strike_style),
                f"{chain.put_ltp[i]:,.2f}",
                iv_str(chain.put_iv[i]),
                _fmt_compact(chain.put_volume[i]),
                _num_text(chain.put_oi_change[i], "{:+,.0f}"),
                Text(_fmt_compact(chain.put_oi[i]) + ("🛡" if p_wall else ""),
                     style=UP if p_wall else ""),
                key=str(strike),
            )

        atm_call_bias = classify_buildup(
            float(chain.call_ltp[atm_i]), float(chain.call_oi_change[atm_i])
        )
        atm_put_bias = classify_buildup(
            float(chain.put_ltp[atm_i]), float(chain.put_oi_change[atm_i])
        )
        unusual_lines = "".join(
            f"\n  · {u['side']} {u['strike']:g} — vol {_fmt_compact(u['volume'])} is "
            f"{u['ratio']:.1f}× OI (fresh positioning)"
            for u in a.unusual[:4]
        ) or " none detected"

        self.query_one("#chain-analytics", Static).update(
            f"[bold {GOLD}]{chain.symbol}[/] spot [bold]{chain.spot:,.2f}[/] · "
            f"expiry {chain.expiry} · lot {chain.lot_size or '—'} · "
            f"source: {chain.source}\n"
            f"[bold]PCR(OI)[/] {a.pcr_oi:.2f} · [bold]PCR(Vol)[/] {a.pcr_volume:.2f} · "
            f"[bold]Max pain[/] {a.max_pain:g} · "
            f"[bold]ATM IV[/] {a.atm_iv:.1%} · "
            f"[bold]Straddle[/] {a.straddle_price:,.0f} → expected move "
            f"[bold]±{a.expected_move_pct:.1%}[/] by expiry\n"
            f"[bold]OI walls:[/] support [{UP}]{a.support:g}[/] (put wall) · "
            f"resistance [{DOWN}]{a.resistance:g}[/] (call wall)\n"
            f"[bold]ATM buildup:[/] calls {atm_call_bias}, puts {atm_put_bias}\n"
            f"[bold]Positioning read:[/] {_bias_markup(a.bias)} — {a.bias_reason}\n"
            f"[bold]Unusual activity:[/]{unusual_lines}\n"
            f"[dim]Heuristic positioning read, not trade advice.[/dim]"
        )
        self.set_status(
            f"{chain.symbol} · {len(chain.strikes)} strikes · {chain.source}"
        )


# ---------------------------------------------------------------------------
# Options payoff
# ---------------------------------------------------------------------------


class PayoffPanel(Panel):
    TITLE = "PAYOFF — `pay NIFTY iron_condor` · `pay NIFTY short_straddle` · custom: `pay NIFTY +23200CE -23400CE`"

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Horizontal(id="pay-layout"):
            yield PlotextPlot(id="pay-plot")
            yield VerticalScroll(Static(id="pay-metrics-body"), id="pay-metrics")

    def show_payoff(self, symbol: str, spec: list[str], width: int = 2) -> None:
        self.set_status(f"Building {' '.join(spec)} on {symbol}…")
        self._load(symbol, spec, width)

    @work(thread=True, exclusive=True, group="payoff")
    def _load(self, symbol: str, spec: list[str], width: int) -> None:
        from shunkan.derivatives import (
            PAYOFF_STRATEGIES,
            analyze_payoff,
            build_strategy,
            parse_custom_legs,
        )

        try:
            chain = get_chain(symbol)
            if len(spec) == 1 and spec[0].lower().replace("-", "_") in PAYOFF_STRATEGIES:
                analysis = build_strategy(chain, spec[0], width=width)
            else:
                legs = parse_custom_legs(chain, spec)
                analysis = analyze_payoff(chain, legs, name="custom")
        except (DataError, ValueError, KeyError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._draw, analysis, chain)

    def _draw(self, a, chain) -> None:
        plot = self.query_one("#pay-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")
        unit = "lot" if a.lot_size else "unit"  # unknown lot -> per-unit money
        x = a.grid.tolist()
        y = (a.payoff_per_unit * (a.lot_size or 1)).tolist()
        plt.plot(x, y, color="orange", label=f"P&L at expiry (per {unit})")
        plt.plot(x, [0.0] * len(x), color="gray")
        plt.vertical_line(a.spot, color="cyan")
        plt.title(
            f"{a.symbol} {a.name} · spot {a.spot:,.0f} (cyan) · expiry {chain.expiry}"
        )
        plot.refresh()

        def money(v: float) -> str:
            if v == float("inf"):
                return "[green]unlimited[/green]"
            if v == float("-inf"):
                return "[red]unlimited[/red]"
            color = "green" if v >= 0 else "red"
            return f"[{color}]₹{v:+,.0f}[/{color}]"

        lines = [f"[bold {GOLD}]{a.symbol} · {a.name}[/] · lot {a.lot_size or '—'}"]
        lines.append("")
        for leg in a.legs:
            lines.append(f"  {leg.describe()}")
        lines.append("")
        prem = a.net_premium * (a.lot_size or 1)
        lines.append(f"[#8b949e]{'Net premium':<16}[/] {'credit' if prem >= 0 else 'debit'} ₹{abs(prem):,.0f}/{unit}")
        lines.append(f"[#8b949e]{'Max profit':<16}[/] {money(a.max_profit)}")
        lines.append(f"[#8b949e]{'Max loss':<16}[/] {money(a.max_loss)}")
        lines.append(f"[#8b949e]{'Risk:reward':<16}[/] "
                     + (f"{a.risk_reward:.2f}" if a.risk_reward != float('inf') else "∞"))
        be = ", ".join(f"{b:,.0f}" for b in a.breakevens) or "none"
        lines.append(f"[#8b949e]{'Breakevens':<16}[/] {be}")
        lines.append(f"[#8b949e]{'POP (model)':<16}[/] {a.pop:.0%} — lognormal @ ATM IV, not market-implied")
        lines.append("")
        lines.append(f"[bold]Position greeks (per {unit})[/]")
        for k in ("delta", "gamma", "theta", "vega"):
            lines.append(f"[#8b949e]{k:<16}[/] {a.greeks[k]:+,.2f}")
        lines.append("")
        lines.append(f"[dim]Premiums from {chain.source}. Excludes margin, fees, slippage.[/dim]")
        self.query_one("#pay-metrics-body", Static).update("\n".join(lines))
        self.set_status(f"{a.name} on {a.symbol} · {len(a.legs)} legs · POP {a.pop:.0%}")


# ---------------------------------------------------------------------------
# Volatility intelligence
# ---------------------------------------------------------------------------


class IVPanel(Panel):
    TITLE = "VOLATILITY — `iv NIFTY` — smile, IV vs realized, expected-move cone"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Horizontal(id="iv-layout"):
            yield PlotextPlot(id="iv-plot")
            yield VerticalScroll(Static(id="iv-metrics-body"), id="iv-metrics")

    def show_iv(self, symbol: str) -> None:
        self.set_status(f"Computing vol analytics for {symbol}…")
        self._load(symbol)

    @work(thread=True, exclusive=True, group="iv")
    def _load(self, symbol: str) -> None:
        from shunkan.derivatives import analyze_vol

        try:
            chain = get_chain(symbol)
            hist = self.provider.history(symbol, period="1y", interval="1d")
            report = analyze_vol(chain, hist)
        except (DataError, ValueError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._draw, report, chain)

    def _draw(self, r, chain) -> None:
        plot = self.query_one("#iv-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")
        strikes = r.smile_strikes.tolist()
        call_iv = [v * 100 if not np.isnan(v) else None for v in r.smile_call_iv]
        put_iv = [v * 100 if not np.isnan(v) else None for v in r.smile_put_iv]
        plt.plot(strikes, call_iv, label="call IV %", color="cyan")
        plt.plot(strikes, put_iv, label="put IV %", color="orange")
        plt.vertical_line(r.spot, color="gray")
        plt.title(f"{r.symbol} IV smile · expiry {chain.expiry} · spot {r.spot:,.0f}")
        plot.refresh()

        lines = [f"[bold {GOLD}]{r.symbol} volatility[/]"]
        lines.append("")
        lines.append(f"[#8b949e]{'ATM IV':<18}[/] {r.atm_iv:.1%}")
        lines.append(f"[#8b949e]{'Realized 21d (CC)':<18}[/] {r.rv_cc_21:.1%}")
        lines.append(f"[#8b949e]{'Realized 21d (Park)':<18}[/] {r.rv_park_21:.1%}")
        prem_color = UP if r.iv_premium > 0 else DOWN
        lines.append(f"[#8b949e]{'IV premium':<18}[/] [{prem_color}]{r.iv_premium * 100:+.1f} vol pts[/]")
        lines.append(f"[#8b949e]{'RV percentile (1y)':<18}[/] {r.rv_percentile:.0%}")
        lines.append("")
        lines.append("[bold]Expected-move cone (±1σ / ±2σ)[/]")
        for days, (lo2, lo1, hi1, hi2) in r.cone.items():
            lines.append(
                f"  {days:>2}d  [{DOWN}]{lo2:,.0f}[/] {lo1:,.0f} ↔ {hi1:,.0f} [{UP}]{hi2:,.0f}[/]"
            )
        if r.notes:
            lines.append("")
            lines.append("[bold]Read[/]")
            lines.extend(f"  · {n}" for n in r.notes)
        self.query_one("#iv-metrics-body", Static).update("\n".join(lines))
        self.set_status(f"{r.symbol} · ATM IV {r.atm_iv:.1%} vs RV {r.rv_cc_21:.1%}")


# ---------------------------------------------------------------------------
# Live tape (streaming ticks)
# ---------------------------------------------------------------------------


class TapePanel(Panel):
    TITLE = "LIVE TAPE — streaming ticks · `tape` (Kite WebSocket when connected, demo feed otherwise)"

    def __init__(self, watchlist: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.watchlist = watchlist
        self._ticker = None
        self._names: dict[int, str] = {}
        self._latest: dict[int, object] = {}
        self._tick_count = 0
        self._started_at = 0.0
        self._known_rows: set[int] = set()

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        yield DataTable(id="tape-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#tape-table", DataTable)
        self._col_keys = table.add_columns("Symbol", "LTP", "Chg%", "Day Range", "Volume", "OI")
        # Paint at 4 Hz regardless of tick arrival rate — the buffer decouples
        # bursty market-hours frames from terminal redraws.
        self.set_interval(0.25, self._paint)

    def start_stream(self) -> None:
        if self._ticker is not None:
            self.set_status("Stream already running.")
            return
        import time as _time

        from shunkan.stream.factory import build_feed

        self._started_at = _time.monotonic()
        feed = build_feed(self.watchlist)
        self._names = dict(feed.names)
        self._ticker = feed.ticker
        feed.ticker.start(feed.tokens, self._on_ticks, mode="quote")
        if feed.live:
            self.set_status(f"Streaming {len(feed.tokens)} instruments from Kite WebSocket…")
        else:
            self.set_status(
                "Demo feed (synthetic random-walk ticks) — connect Zerodha for the real tape."
            )

    def stop_stream(self) -> None:
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None

    def _on_ticks(self, ticks) -> None:
        # Called from the ticker thread — just buffer; painting happens on
        # the UI timer. Dict assignment is atomic under the GIL.
        for t in ticks:
            self._latest[t.token] = t
        self._tick_count += len(ticks)

    def _paint(self) -> None:
        if not self._latest:
            return
        import time as _time

        table = self.query_one("#tape-table", DataTable)
        for token, tick in list(self._latest.items()):
            name = self._names.get(token, str(token))
            rng = (
                f"{tick.low:,.1f} – {tick.high:,.1f}"
                if tick.high > 0 else "—"
            )
            cells = (
                Text(name, style="bold"),
                f"{tick.ltp:,.2f}",
                _pct_text(tick.change_pct),
                rng,
                _fmt_compact(tick.volume) if tick.volume else "—",
                _fmt_compact(tick.oi) if tick.oi else "—",
            )
            if token in self._known_rows:
                for key, value in zip(self._col_keys, cells):
                    table.update_cell(str(token), key, value, update_width=False)
            else:
                table.add_row(*cells, key=str(token))
                self._known_rows.add(token)
        elapsed = max(_time.monotonic() - self._started_at, 1e-9)
        rate = self._tick_count / elapsed
        self.set_title(
            f"LIVE TAPE — {self._tick_count:,} ticks · {rate:,.0f}/sec sustained"
        )


# ---------------------------------------------------------------------------
# Volume intelligence
# ---------------------------------------------------------------------------


class VolumePanel(Panel):
    TITLE = "VOLUME — `vol RELIANCE` · `vol NIFTY 1y` — profile, surges, OBV flow"

    def __init__(self, provider, **kwargs) -> None:
        super().__init__(**kwargs)
        self.provider = provider

    def compose(self) -> ComposeResult:
        yield from self.compose_header()
        with Horizontal(id="vol-layout"):
            yield PlotextPlot(id="vol-plot")
            yield VerticalScroll(Static(id="vol-metrics-body"), id="vol-metrics")

    def show_volume(self, symbol: str, period: str = "6mo") -> None:
        self.set_status(f"Analyzing volume for {symbol} ({period})…")
        self._load(symbol, period)

    @work(thread=True, exclusive=True, group="volume")
    def _load(self, symbol: str, period: str) -> None:
        try:
            hist = self.provider.history(symbol, period=period, interval="1d")
            report = analyze_volume(hist)
        except (DataError, ValueError) as exc:
            self.app.call_from_thread(self.set_status, f"[red]{exc}[/red]")
            return
        self.app.call_from_thread(self._draw, symbol, period, hist, report)

    def _draw(self, symbol: str, period: str, hist, report) -> None:
        plot = self.query_one("#vol-plot", PlotextPlot)
        plt = plot.plt
        plt.clear_figure()
        plt.theme("dark")
        prof = report.profile
        mids = 0.5 * (prof.bin_edges[:-1] + prof.bin_edges[1:])
        labels = [f"{m:,.0f}" for m in mids]
        colors = [
            "orange" if abs(m - prof.poc) < 1e-9 or
            (prof.value_area_low <= m <= prof.value_area_high) else "gray"
            for m in mids
        ]
        plt.bar(labels, prof.volume_at_price.tolist(), orientation="horizontal",
                color="orange")
        plt.title(
            f"{symbol.upper()} volume-by-price ({period}) · "
            f"POC {prof.poc:,.0f} · value area {prof.value_area_low:,.0f}–{prof.value_area_high:,.0f}"
        )
        plot.refresh()

        last_close = float(hist["close"].iloc[-1])
        lines = [
            f"[bold {GOLD}]{symbol.upper()}[/] last [bold]{last_close:,.2f}[/]",
            "",
            f"[#8b949e]{'Day type':<14}[/] {report.day_type}",
            f"[#8b949e]{'Volume z':<14}[/] {report.surge_z:+.2f}σ vs 20-bar",
            f"[#8b949e]{'Volume ratio':<14}[/] {report.surge_ratio:.2f}× average",
            f"[#8b949e]{'OBV signal':<14}[/] {report.obv_divergence}",
            f"[#8b949e]{'POC':<14}[/] {prof.poc:,.0f}",
            f"[#8b949e]{'Value area':<14}[/] {prof.value_area_low:,.0f} – {prof.value_area_high:,.0f}",
        ]
        if report.notes:
            lines.append("")
            lines.append("[bold]Signals[/]")
            lines.extend(f"  · {n}" for n in report.notes)
        self.query_one("#vol-metrics-body", Static).update("\n".join(lines))
        self.set_status(f"{symbol.upper()} · {len(hist)} bars analyzed")

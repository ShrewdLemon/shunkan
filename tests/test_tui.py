"""TUI smoke tests using Textual's pilot (offline synthetic data)."""

import pytest

from shunkan.data.provider import SyntheticProvider
from shunkan.portfolio import Portfolio
from shunkan.ui.app import ShunkanApp
from textual.widgets import ContentSwitcher, Input


@pytest.fixture
def app(tmp_path):
    return ShunkanApp(
        provider=SyntheticProvider(),
        portfolio=Portfolio(cash=50_000.0, path=tmp_path / "pf.json"),
    )


async def _submit(pilot, command: str) -> None:
    box = pilot.app.query_one("#command-input", Input)
    box.focus()
    box.value = command
    await pilot.press("enter")
    await pilot.pause()


async def test_app_boots_to_pulse(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        switcher = app.query_one("#main-switcher", ContentSwitcher)
        assert switcher.current == "pulse"


async def test_option_chain_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "oc NIFTY")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "chain"
        await pilot.pause(1.0)


async def test_volume_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "vol RELIANCE")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "volume"
        await pilot.pause(1.0)


async def test_watchlist_panel_via_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "w")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "dashboard"


async def test_help_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "help")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "help"


async def test_chart_command_switches_panel(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "c AAPL 3mo 1d")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "chart"
        # allow the worker to render
        await pilot.pause(0.5)


async def test_quote_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "q MSFT")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "quote"
        await pilot.pause(0.5)


async def test_backtest_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "bt AAPL sma_cross fast=10 slow=50 1y")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "backtest"
        await pilot.pause(1.0)


async def test_screener_command(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "scr mega rsi>0")
        assert app.query_one("#main-switcher", ContentSwitcher).current == "screener"
        await pilot.pause(1.0)


async def test_paper_trade_flow(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "buy AAPL 10 100")
        await pilot.pause(0.5)
        assert app.portfolio.positions["NSE:AAPL"].quantity == 10
        await _submit(pilot, "sell AAPL 10 110")
        await pilot.pause(0.5)
        assert app.portfolio.realized_pnl == pytest.approx(100.0)


async def test_unknown_command_does_not_crash(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await _submit(pilot, "frobnicate AAPL")
        # app should still be running with the home panel intact
        assert app.query_one("#main-switcher", ContentSwitcher).current == "pulse"


async def test_fkey_navigation(app):
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("f4")
        await pilot.pause()
        assert app.query_one("#main-switcher", ContentSwitcher).current == "backtest"

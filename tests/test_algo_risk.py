"""Tests for the risk governor and paper-trading mode.

These matter more than the backtest tests. A wrong backtest costs you a bad
idea; a governor that fails open costs you the account. Every limit gets a
test that proves it REFUSES, and the latch gets a test that proves it will
not let go on its own.
"""

from __future__ import annotations

import ast
import datetime
import itertools
import random
import time
from pathlib import Path

import pytest

from shunkan.algo import (
    BacktestConfig,
    BaseStrategy,
    Divergence,
    PaperConfig,
    PaperTrader,
    Reason,
    RiskGovernor,
    RiskLimits,
    RiskState,
    Side,
    TickRecorder,
    backtest,
    compare,
)
from shunkan.stream.parser import build_frame, build_full_packet, parse_binary

BIDS = [(158.10, 65, 1), (158.00, 1105, 3)]
ASKS = [(159.60, 130, 1), (159.65, 195, 1)]


def tick(ltp=158.25, bids=BIDS, asks=ASKS, token=1, volume=0):
    return parse_binary(
        build_frame([build_full_packet(token, ltp, volume=volume, bids=bids, asks=asks)])
    )[0]


def noon() -> float:
    n = datetime.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    return n.timestamp()


def gov(**kw) -> RiskGovernor:
    g = RiskGovernor(RiskLimits(**kw))
    g.observe_tick(tick(), 0)
    return g


def ask(g, qty=65, limit=158.10, position=0, t=None, tk=None):
    return g.check(1, Side.BUY, qty, limit, tk or tick(), position, 0, now=t or noon())


# --------------------------------------------------------------------------
# each limit must refuse
# --------------------------------------------------------------------------

def test_a_normal_order_is_allowed():
    assert ask(gov()).allowed


def test_order_size_limit():
    assert ask(gov(max_order_qty=65), qty=650).reason is Reason.ORDER_SIZE


def test_zero_or_negative_quantity_refused():
    assert not ask(gov(), qty=0).allowed
    assert not ask(gov(), qty=-65).allowed


def test_position_limit_uses_projected_not_current():
    g = gov(max_position=195, max_order_qty=65)
    assert ask(g, position=130).allowed          # 130 + 65 = 195, at the line
    assert ask(g, position=195).reason is Reason.POSITION_LIMIT


def test_position_limit_applies_to_shorts_too():
    g = gov(max_position=195, max_order_qty=65)
    d = g.check(1, Side.SELL, 65, 159.60, tick(), -195, 0, now=noon())
    assert d.reason is Reason.POSITION_LIMIT


def test_fat_finger_price_band():
    assert ask(gov(price_band_pct=0.05), limit=900.0).reason is Reason.PRICE_BAND
    assert ask(gov(price_band_pct=0.05), limit=158.10).allowed


def test_broken_book_refused():
    crossed = tick(bids=[(160.0, 65, 1)], asks=[(159.0, 65, 1)])
    assert ask(gov(), tk=crossed).reason is Reason.BROKEN_BOOK


def test_gross_exposure_limit():
    g = gov(max_gross_exposure=1000.0, max_order_qty=65)
    assert ask(g).reason is Reason.GROSS_EXPOSURE


def test_outside_session_window_refused():
    g = gov()
    assert ask(g, t=noon() - 6 * 3600).reason is Reason.OUTSIDE_WINDOW   # 06:00
    assert ask(g, t=noon() + 5 * 3600).reason is Reason.OUTSIDE_WINDOW   # 17:00


def test_default_window_closes_before_the_auction():
    """15:25 default, not 15:40 — the pre-auction stretch has no live cash
    reference and the near-month future can move 25+ points against you."""
    assert RiskLimits().session_close < datetime.time(15, 30)


def test_stale_feed_refused():
    g = RiskGovernor(RiskLimits(stale_feed_seconds=5.0))
    g.observe_tick(tick(), 0)
    d = g.check(1, Side.BUY, 65, 158.10, tick(), 0, 10 * 10**9, now=noon())
    assert d.reason is Reason.STALE_FEED


def test_orders_per_second_cap_is_under_the_sebi_hft_threshold():
    assert RiskLimits().max_orders_per_second < 10


def test_order_rate_per_second():
    g = gov(max_orders_per_second=3)
    t = noon()
    assert sum(1 for _ in range(3) if ask(g, t=t).allowed) == 3
    assert ask(g, t=t).reason is Reason.ORDER_RATE


def test_order_rate_per_minute():
    g = gov(max_orders_per_second=1000, max_orders_per_minute=5)
    t = noon()
    for i in range(5):
        assert ask(g, t=t + i).allowed
    assert ask(g, t=t + 6).reason is Reason.ORDER_RATE


def test_daily_order_cap():
    g = gov(max_orders_per_day=3, max_orders_per_second=1000)
    t = noon()
    for i in range(3):
        assert ask(g, t=t + i * 10).allowed
    assert ask(g, t=t + 100).reason is Reason.DAILY_ORDER_COUNT


def test_kill_file_halts_everything(tmp_path: Path):
    kf = tmp_path / "HALT"
    g = gov(kill_file=kf)
    assert ask(g).allowed
    kf.write_text("stop")
    assert ask(g).reason is Reason.KILL_FILE


# --------------------------------------------------------------------------
# the latch — the most important behaviour in the module
# --------------------------------------------------------------------------

def test_daily_loss_trips_the_governor():
    g = gov(max_daily_loss=5000)
    g.observe_equity(-5000.01)
    assert g.state is RiskState.TRIPPED
    assert g.stats.trip_reason is Reason.DAILY_LOSS


def test_drawdown_from_peak_trips():
    g = gov(max_daily_loss=10**9, max_drawdown=2000)
    g.observe_equity(5000)      # peak
    g.observe_equity(2999)      # -2001 from peak
    assert g.stats.trip_reason is Reason.DRAWDOWN


def test_consecutive_losses_trip():
    g = gov(max_consecutive_losses=3)
    for _ in range(3):
        g.observe_round_trip(-100)
    assert g.stats.trip_reason is Reason.CONSECUTIVE_LOSSES


def test_a_win_resets_the_losing_streak():
    g = gov(max_consecutive_losses=3)
    g.observe_round_trip(-100)
    g.observe_round_trip(-100)
    g.observe_round_trip(+50)
    g.observe_round_trip(-100)
    assert g.armed


def test_tripped_governor_refuses_every_order():
    g = gov(max_daily_loss=1000)
    g.observe_equity(-2000)
    assert ask(g).reason is Reason.TRIPPED


def test_recovery_does_not_auto_rearm():
    """The single most important line in risk.py. A governor that unlatches
    when P&L recovers turns a controlled loss into an uncontrolled one."""
    g = gov(max_daily_loss=1000)
    g.observe_equity(-2000)
    g.observe_equity(+5000)
    assert g.state is RiskState.TRIPPED
    assert not ask(g).allowed


def test_rearm_requires_a_named_human():
    g = gov(max_daily_loss=1000)
    g.observe_equity(-2000)
    with pytest.raises(ValueError):
        g.rearm("")
    with pytest.raises(ValueError):
        g.rearm("   ")
    g.rearm("punarbasu")
    assert g.armed


def test_trip_keeps_the_first_reason():
    g = gov(max_daily_loss=1000, max_drawdown=100)
    g.observe_equity(-2000)
    first = g.stats.trip_reason
    g.observe_equity(-9000)
    assert g.stats.trip_reason is first


def test_rejections_are_counted_by_reason():
    g = gov(max_order_qty=65)
    for _ in range(3):
        ask(g, qty=650)
    assert g.stats.rejections["ORDER_SIZE"] == 3
    assert g.stats.orders_rejected == 3


# --------------------------------------------------------------------------
# engine integration — the governor cannot be routed around
# --------------------------------------------------------------------------

def _write_session(tmp_path, n=2000, seed=7):
    clk = itertools.count(1_000_000_000_000, 2_000_000)
    rng = random.Random(seed)
    px, vol = 158.25, 1_000_000
    with TickRecorder(
        tokens=[1], root=tmp_path, session="s", flush_rows=500, clock=lambda: next(clk)
    ) as rec:
        for _ in range(n):
            px = max(5.0, px + rng.gauss(0, 0.08))
            vol += rng.randint(0, 400)
            b, a = round(px - 0.75, 2), round(px + 0.75, 2)
            ltp = b if rng.random() < 0.5 else a
            rec.on_ticks(parse_binary(build_frame([build_full_packet(
                1, ltp, volume=vol,
                bids=[(b, 65, 1), (round(b - 0.05, 2), 1105, 3)],
                asks=[(a, 130, 1), (round(a + 0.05, 2), 195, 1)],
            )])))


class Spammer(BaseStrategy):
    name = "spammer"

    def on_tick(self, ctx):
        ctx.buy(65)


def test_governor_blocks_orders_at_the_engine(tmp_path):
    _write_session(tmp_path, n=500)
    cfg = BacktestConfig(session="s", root=tmp_path, tokens=[1])
    g = RiskGovernor(RiskLimits(max_position=65, max_order_qty=65,
                                session_open=datetime.time(0, 0),
                                session_close=datetime.time(23, 59)))
    r = backtest(cfg, Spammer(), g)
    assert r.orders_rejected > 0
    assert r.orders < 500            # most were refused
    assert g.stats.rejections


def test_rejected_orders_never_reach_the_fill_engine(tmp_path):
    _write_session(tmp_path, n=300)
    cfg = BacktestConfig(session="s", root=tmp_path, tokens=[1])
    g = RiskGovernor(RiskLimits(max_order_qty=1))   # every 65-lot order refused
    r = backtest(cfg, Spammer(), g)
    assert r.orders == 0
    assert r.fills == 0
    assert r.orders_rejected == 300


def test_a_backtest_can_be_halted_by_the_loss_limit(tmp_path):
    _write_session(tmp_path, n=2000)
    cfg = BacktestConfig(session="s", root=tmp_path, tokens=[1])
    g = RiskGovernor(RiskLimits(max_daily_loss=1.0, max_drawdown=1.0,
                                session_open=datetime.time(0, 0),
                                session_close=datetime.time(23, 59)))
    r = backtest(cfg, Spammer(), g)
    assert g.tripped
    assert r.orders_rejected > 0


# --------------------------------------------------------------------------
# paper mode
# --------------------------------------------------------------------------

def test_paper_module_has_no_path_to_placing_an_order():
    """Structural, via AST — a text grep matches the docstring that promises
    this and would pass for the wrong reason."""
    tree = ast.parse(Path("src/shunkan/algo/paper.py").read_text())
    imports, attrs = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imports.add(n.module or "")
        elif isinstance(n, ast.Attribute):
            attrs.add(n.attr)
    banned_modules = {"shunkan.data.brokers", "kiteconnect", "httpx", "requests"}
    banned_attrs = {"place_order", "modify_order", "cancel_order", "place_gtt_order"}
    assert not {i for i in imports if any(i.startswith(b) for b in banned_modules)}
    assert not (attrs & banned_attrs)


def test_paper_trader_drives_the_engine_from_ticks(tmp_path):
    class BuyOnce(BaseStrategy):
        name = "buy-once"

        def __init__(self):
            self.done = False

        def on_tick(self, ctx):
            if not self.done:
                ctx.buy(65)
                self.done = True

    g = RiskGovernor(RiskLimits(session_open=datetime.time(0, 0),
                                session_close=datetime.time(23, 59)))
    pt = PaperTrader(
        BuyOnce(),
        PaperConfig(tokens=[1], session="p", root=tmp_path, record=True),
        governor=g,
    )
    pt.recorder.start()
    pt._running = True
    for i in range(50):
        pt.on_ticks([tick(volume=1000 + i * 10)])
    r = pt.stop()
    assert r.ticks == 50
    assert r.orders >= 1
    assert pt.status["ticks"] == 50


def test_paper_records_while_it_trades(tmp_path):
    """A paper run must leave a replayable tape, or it cannot be compared
    against a backtest — which is the entire point of running it."""
    from shunkan.algo import replay

    pt = PaperTrader(
        BaseStrategy.__new__(type("Noop", (BaseStrategy,), {"on_tick": lambda s, c: None})),
        PaperConfig(tokens=[1], session="p", root=tmp_path, record=True),
        governor=RiskGovernor(RiskLimits()),
    )
    pt.recorder.start()
    pt._running = True
    for i in range(30):
        pt.on_ticks([tick(volume=i)])
    pt.stop()
    assert len(list(replay("p", root=tmp_path))) == 30


def test_strategy_exception_trips_rather_than_crashes(tmp_path):
    class Broken(BaseStrategy):
        name = "broken"

        def on_tick(self, ctx):
            raise RuntimeError("boom")

    g = RiskGovernor(RiskLimits())
    pt = PaperTrader(
        Broken(), PaperConfig(tokens=[1], session="p", root=tmp_path, record=False), g
    )
    pt._running = True
    pt.on_ticks([tick()])          # must not raise
    assert g.tripped
    assert "boom" in g.stats.trip_detail


def test_divergence_flags_an_optimistic_backtest():
    d = Divergence(-1200.0, 850.0, 40, 64, 0.55, 0.91)
    assert d.optimistic
    assert d.net_gap == pytest.approx(-2050.0)
    assert "OPTIMISTIC" in d.summary()


def test_divergence_when_paper_beats_backtest():
    d = Divergence(500.0, 100.0, 60, 60, 0.9, 0.9)
    assert not d.optimistic


# --------------------------------------------------------------------------
# a trip must close risk, not merely stop adding to it
# --------------------------------------------------------------------------

def test_flatten_on_trip_is_the_default():
    """Without it a loss limit is advisory: new orders stop but the position
    you already hold keeps marking against you."""
    assert RiskLimits().flatten_on_trip is True


def test_trip_closes_the_open_position(tmp_path):
    _write_session(tmp_path, n=3000, seed=11)
    cfg = BacktestConfig(session="s", root=tmp_path, tokens=[1])
    limits = dict(
        max_daily_loss=300, max_drawdown=300, max_position=195, max_order_qty=65,
        session_open=datetime.time(0, 0), session_close=datetime.time(23, 59),
    )
    from shunkan.algo import QuoteBothSides

    g_off = RiskGovernor(RiskLimits(flatten_on_trip=False, **limits))
    r_off = backtest(cfg, QuoteBothSides(edge=1.0, size=65, max_position=195), g_off)
    g_on = RiskGovernor(RiskLimits(flatten_on_trip=True, **limits))
    r_on = backtest(cfg, QuoteBothSides(edge=1.0, size=65, max_position=195), g_on)

    assert g_off.tripped and g_on.tripped
    assert r_on.flattened_on_trip is True
    assert r_off.flattened_on_trip is False
    # Closing on the trip must not make the realised drawdown worse.
    assert r_on.max_drawdown >= r_off.max_drawdown


def test_governor_owns_no_execution_surface():
    """The governor decides; the engine acts. Keeping execution out of the
    governor is what lets it be trusted and unit-tested in isolation."""
    import ast

    tree = ast.parse(Path("src/shunkan/algo/risk.py").read_text())
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imports.add(n.module or "")
        elif isinstance(n, ast.Import):
            imports.update(a.name for a in n.names)
    assert "shunkan.algo.engine" not in imports
    assert not {i for i in imports if i.startswith("shunkan.data")}


# --------------------------------------------------------------------------
# governed backtests run on event time
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_tz():
    yield
    time.tzset()


def _write_session_at(tmp_path, start_ns, n=400):
    clk = itertools.count(start_ns, 50_000_000)
    rng = random.Random(3)
    px, vol = 158.25, 1_000_000
    with TickRecorder(tokens=[1], root=tmp_path, session="t", flush_rows=200,
                      clock=lambda: next(clk)) as rec:
        for _ in range(n):
            px = max(5.0, px + rng.gauss(0, 0.08))
            vol += rng.randint(0, 400)
            b, a = round(px - 0.75, 2), round(px + 0.75, 2)
            rec.on_ticks(parse_binary(build_frame([build_full_packet(
                1, b, volume=vol, bids=[(b, 65, 1)], asks=[(a, 130, 1)])])))


def _ist_ns(y, mo, d, h, mi):
    return int(datetime.datetime(y, mo, d, h, mi,
               tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).timestamp() * 1e9)


class OneEvery100(BaseStrategy):
    name = "one-every-100"

    def __init__(self):
        self.n = 0

    def on_tick(self, ctx):
        self.n += 1
        if self.n % 100 == 0 and ctx.position == 0:
            ctx.buy(65)


def test_governed_backtest_uses_tick_time_not_wall_time(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    _write_session_at(tmp_path, _ist_ns(2026, 11, 5, 10, 0))
    g = RiskGovernor(RiskLimits(), clock=lambda: _ist_ns(2026, 11, 5, 21, 0) / 1e9)
    r = backtest(BacktestConfig(session="t", root=tmp_path, tokens=[1]), OneEvery100(), g)
    assert r.orders > 0


def test_governed_backtest_refuses_ticks_after_the_close(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    _write_session_at(tmp_path, _ist_ns(2026, 11, 5, 16, 0))
    g = RiskGovernor(RiskLimits(), clock=lambda: _ist_ns(2026, 11, 5, 11, 0) / 1e9)
    r = backtest(BacktestConfig(session="t", root=tmp_path, tokens=[1]), OneEvery100(), g)
    assert r.orders == 0 and r.orders_rejected > 0


def test_event_clock_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    _write_session_at(tmp_path, _ist_ns(2026, 11, 5, 16, 0))
    g = RiskGovernor(RiskLimits(), clock=lambda: _ist_ns(2026, 11, 5, 11, 0) / 1e9)
    cfg = BacktestConfig(session="t", root=tmp_path, tokens=[1], event_clock=False)
    r = backtest(cfg, OneEvery100(), g)
    assert r.orders > 0

import datetime
import itertools
import json
import math
import random

import pytest

import shunkan.stream.ticker as ticker_mod
from shunkan.algo import (BacktestConfig, BaseStrategy, Engine, NullStrategy, PaperConfig,
                          PaperTrader, QuoteBothSides, Reason, RiskGovernor, RiskLimits,
                          save_report, strategy_params)
from shunkan.algo.paper import replay_gap
from shunkan.stream.parser import build_frame, build_full_packet, parse_binary

ALL_DAY = dict(session_open=datetime.time(0, 0), session_close=datetime.time(23, 59))
T0 = 1_000_000_000_000


def _book(bid, ask, volume, token=1):
    return parse_binary(build_frame([build_full_packet(
        token, round((bid + ask) / 2, 2), volume=volume,
        bids=[(bid, 650, 3)], asks=[(ask, 650, 3)])]))[0]


def _tape(n):
    return [_book(100.5, 101.5, 1000 + 100 * i) for i in range(n)]


class SellOnce(BaseStrategy):
    name = "sell-once"

    def __init__(self, qty=65):
        self.qty = qty
        self.sent = None

    def on_tick(self, ctx):
        if self.sent is None:
            self.sent = ctx.sell(self.qty)


class BuyOnce(BaseStrategy):
    name = "buy-once"

    def __init__(self):
        self.sent = None

    def on_tick(self, ctx):
        if self.sent is None:
            self.sent = ctx.buy(65)


def _run(engine, tape):
    for i, t in enumerate(tape):
        engine.step(t, T0 + 100_000_000 * i)
    return engine.finalize()


def test_opening_position_is_marked_before_any_order():
    e = Engine(BacktestConfig(session="t", opening_qty=65, opening_price=100.0,
                              flatten_at_end=False), NullStrategy())
    r = _run(e, _tape(3))
    assert r.orders == 0
    assert [eq for _, eq in r.equity_curve] == [65.0, 65.0, 65.0]
    assert r.open_position == 65 and r.open_avg_price == 100.0


def test_selling_the_opening_closes_it_at_the_opening_price():
    e = Engine(BacktestConfig(session="t", opening_qty=65, opening_price=100.0), SellOnce())
    r = _run(e, _tape(5))
    assert len(r.round_trips) == 1
    trip = r.round_trips[0]
    assert trip.entry == 100.0 and trip.exit == 100.5 and trip.quantity == 65
    assert trip.direction == 1
    assert e.pos.quantity == 0 and r.open_position == 0


def test_governor_counts_the_opening_position():
    g = RiskGovernor(RiskLimits(max_position=65, **ALL_DAY))
    strat = BuyOnce()
    e = Engine(BacktestConfig(session="t", opening_qty=65, opening_price=100.0,
                              flatten_at_end=False), strat, g)
    r = _run(e, _tape(3))
    assert r.orders_rejected == 1 and r.orders == 0
    assert e.last_rejection.reason is Reason.POSITION_LIMIT
    assert e.pos.quantity == 65


@pytest.mark.parametrize("price", [0.0, -1.0, math.nan, math.inf])
def test_opening_without_a_sane_price_is_refused(price):
    with pytest.raises(ValueError):
        Engine(BacktestConfig(session="t", opening_qty=65, opening_price=price), NullStrategy())


def test_flat_opening_ignores_the_price():
    e = Engine(BacktestConfig(session="t", opening_qty=0, opening_price=123.0), NullStrategy())
    assert e.pos.quantity == 0 and e.pos.avg_price == 0.0


def _ticks(n=3000, seed=11):
    rng = random.Random(seed)
    px, vol = 158.25, 1_000_000
    for _ in range(n):
        px = max(5.0, px + rng.gauss(0, 0.08))
        vol += rng.randint(0, 400)
        b, a = round(px - 0.75, 2), round(px + 0.75, 2)
        ltp = b if rng.random() < 0.5 else a
        yield parse_binary(build_frame([build_full_packet(
            1, ltp, volume=vol, bids=[(b, 65, 1), (round(b - 0.05, 2), 1105, 3)],
            asks=[(a, 130, 1), (round(a + 0.05, 2), 195, 1)])]))


def _paper_session(tmp_path, ecfg):
    stamps = itertools.count(T0, 2_000_000)
    strat = QuoteBothSides(edge=1.0, size=65, max_position=195)
    g = RiskGovernor(RiskLimits(**ALL_DAY), clock=lambda: 0.0)
    pt = PaperTrader(strat, PaperConfig(tokens=[1], session="p", root=tmp_path,
                                        clock=lambda: next(stamps)), governor=g,
                     engine_config=ecfg)
    pt.recorder.start()
    pt._running = True
    for batch in _ticks():
        pt.on_ticks(batch)
    r = pt.stop()
    save_report(r, g, tmp_path / "p.report.json", strategy_spec="quote-both-sides",
                strategy_params=strategy_params(strat), tokens=[1], watch=[], segment="option",
                engine_config=pt.engine.cfg)
    return r


def test_report_round_trips_a_carried_session(tmp_path):
    ecfg = BacktestConfig(session="p", opening_qty=65, opening_price=157.0, flatten_at_end=False)
    r = _paper_session(tmp_path, ecfg)
    rep = json.loads((tmp_path / "p.report.json").read_text())
    assert rep["opening_qty"] == 65 and rep["opening_price"] == 157.0
    gap = replay_gap("p", root=tmp_path)
    assert r.fills > 0
    assert gap.net_gap == 0
    assert gap.matched_fills == gap.paper_fills == gap.replay_fills
    del rep["opening_qty"], rep["opening_price"]
    (tmp_path / "p.report.json").write_text(json.dumps(rep))
    assert replay_gap("p", root=tmp_path).net_gap != 0


def test_report_without_opening_keys_still_replays(tmp_path):
    r = _paper_session(tmp_path, BacktestConfig(session="p", flatten_at_end=False))
    path = tmp_path / "p.report.json"
    rep = json.loads(path.read_text())
    del rep["opening_qty"], rep["opening_price"]
    path.write_text(json.dumps(rep))
    gap = replay_gap("p", root=tmp_path)
    assert r.fills > 0
    assert gap.net_gap == 0 and gap.matched_fills == gap.paper_fills


class FakeTicker:
    def __init__(self):
        self.calls = []
        self.stopped = False
        self.cb = None

    def start(self, tokens, callback, mode="quote"):
        self.calls.append((list(tokens), mode))
        self.cb = callback

    def stop(self):
        self.stopped = True


class NoKite:
    def __init__(self, *a, **k):
        raise AssertionError("KiteTicker used")


def test_injected_ticker_replaces_the_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(ticker_mod, "KiteTicker", NoKite)
    stamps = itertools.count(T0, 100_000_000)
    pt = PaperTrader(NullStrategy(), PaperConfig(tokens=[1], watch=[2], session="p", root=tmp_path,
                                                 clock=lambda: next(stamps)))
    fake = FakeTicker()
    pt.start("key", "token", ticker=fake)
    assert fake.calls == [([1, 2], "full")]
    assert fake.cb == pt.on_ticks
    fake.cb([_book(100.5, 101.5, 1000), _book(10.0, 10.5, 50, token=2)])
    fake.cb([_book(100.5, 101.5, 1100)])
    r = pt.stop()
    assert fake.stopped
    assert r.ticks == 2
    assert pt.recorder.stats.ticks_received == 3


def test_run_until_passes_the_ticker_through(tmp_path, monkeypatch):
    monkeypatch.setattr(ticker_mod, "KiteTicker", NoKite)
    pt = PaperTrader(NullStrategy(), PaperConfig(tokens=[1], session="p", root=tmp_path))
    fake = FakeTicker()
    r = pt.run_until("key", "token", 0, ticker=fake)
    assert fake.calls == [([1], "full")] and fake.stopped and r.ticks == 0

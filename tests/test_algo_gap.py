import dataclasses
import datetime
import itertools
import json
import random

import pandas as pd
import pytest

from shunkan.algo import (BacktestConfig, BaseStrategy, Engine, FillConfig, OrderStatus,
                          PaperConfig, PaperTrader, QuoteBothSides, Rates, RiskGovernor,
                          RiskLimits, replay, save_report, strategy_params)
from shunkan.algo.chain import select_chain
from shunkan.algo.paper import append_gap, match_fills, replay_gap
from shunkan.stream.parser import DepthLevel, build_frame, build_full_packet, parse_binary


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


def _paper_session(tmp_path, limits, strat=None, spec="quote-both-sides", engine_config=None):
    stamps = itertools.count(1_000_000_000_000, 2_000_000)
    strat = strat or QuoteBothSides(edge=1.0, size=65, max_position=195)
    g = RiskGovernor(limits, clock=lambda: 0.0)
    pt = PaperTrader(strat, PaperConfig(tokens=[1], session="p", root=tmp_path,
                                        clock=lambda: next(stamps)), governor=g,
                     engine_config=engine_config)
    pt.recorder.start()
    pt._running = True
    for batch in _ticks():
        pt.on_ticks(batch)
    r = pt.stop()
    g.use_clock(lambda: 0.0)
    save_report(r, g, tmp_path / "p.report.json", strategy_spec=spec,
                strategy_params=strategy_params(strat), tokens=[1], watch=[], segment="option",
                engine_config=pt.engine.cfg)
    return r


ALL_DAY = dict(session_open=datetime.time(0, 0), session_close=datetime.time(23, 59))


def test_identical_tape_and_setup_gives_zero_gap(tmp_path):
    r = _paper_session(tmp_path, RiskLimits(**ALL_DAY))
    gap = replay_gap("p", root=tmp_path)
    assert r.fills > 0
    assert gap.replay_fills == gap.paper_fills
    assert gap.matched_fills == gap.paper_fills
    assert abs(gap.net_gap) < 1e-6
    assert gap.first_divergence_ns is None
    assert gap.paper_tripped is False


def test_replay_applies_the_same_limits(tmp_path):
    lim = RiskLimits(max_daily_loss=300, max_drawdown=300, **ALL_DAY)
    _paper_session(tmp_path, lim)
    gap = replay_gap("p", root=tmp_path)
    assert gap.paper_tripped
    assert gap.paper_tripped == gap.replay_tripped


def test_match_fills_flags_first_divergence():
    a = [(10, 1, "BUY", 100.0, 65, True), (20, 1, "SELL", 101.0, 65, True)]
    b = [(10, 1, "BUY", 100.0, 65, True), (25, 1, "SELL", 101.5, 65, True)]
    n, first = match_fills(a, b)
    assert n == 1 and first == 20


def test_gap_ledger_appends_lines(tmp_path):
    _paper_session(tmp_path, RiskLimits(**ALL_DAY))
    gap = replay_gap("p", root=tmp_path)
    led = tmp_path / "gaps.jsonl"
    append_gap(gap, led)
    append_gap(gap, led)
    rows = [json.loads(x) for x in led.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["session"] == "p" and "match_rate" in rows[0]


def test_select_chain_takes_nearest_expiry_and_window():
    rows = []
    tok = 1000
    for exp in ("2026-11-10", "2026-11-17"):
        for k in range(23000, 25001, 50):
            for r in ("CE", "PE"):
                tok += 1
                rows.append(dict(instrument_token=tok, name="NIFTY", instrument_type=r,
                                 segment="NFO-OPT", expiry=exp, strike=float(k)))
    df = pd.DataFrame(rows)
    toks = select_chain(df, center=24012.0, n=2, today=datetime.date(2026, 11, 5))
    picked = df[df.instrument_token.isin(toks)]
    assert set(picked.expiry) == {"2026-11-10"}
    assert sorted(set(picked.strike)) == [23900.0, 23950.0, 24000.0, 24050.0, 24100.0]
    assert len(picked) == 10
    assert 256265 in toks and 264969 in toks


def test_compare_cli_prints_the_gap_and_logs_it(tmp_path, monkeypatch, capsys):
    import shunkan.algo.__main__ as cli

    _paper_session(tmp_path, RiskLimits(**ALL_DAY))
    monkeypatch.setattr(cli, "TICKS_DIR", tmp_path)
    assert cli.main(["compare", "p"]) == 0
    assert cli.main(["compare", "p"]) == 0
    out = capsys.readouterr().out
    assert "determinism and setup, not fill realism" in out
    assert len((tmp_path / "gaps.jsonl").read_text().splitlines()) == 2


def test_recorder_and_engine_share_one_stamp_per_batch(tmp_path):
    seen = []

    class Spy(BaseStrategy):
        name = "spy"

        def on_tick(self, ctx):
            seen.append(ctx.ts_ns)

    pt = PaperTrader(Spy(), PaperConfig(tokens=[1], session="p", root=tmp_path),
                     governor=RiskGovernor(RiskLimits()))
    pt.recorder.start()
    pt._running = True
    for batch in itertools.islice(_ticks(), 200):
        pt.on_ticks(batch)
    pt.stop()
    assert [e.ts_ns for e in replay("p", root=tmp_path)] == seen


def test_replay_ignores_the_kill_file(tmp_path):
    kill = tmp_path / "halt"
    kill.touch()
    _paper_session(tmp_path, RiskLimits(kill_file=kill, **ALL_DAY))
    gap = replay_gap("p", root=tmp_path)
    assert gap.paper_tripped and not gap.replay_tripped and gap.replay_fills > 0


def test_replay_rebuilds_a_non_default_engine_setup(tmp_path):
    ecfg = BacktestConfig(session="p", fills=FillConfig(latency_ns=120_000_000, participation=0.25),
                          flatten_at_end=False, rates=Rates(brokerage_flat=1.0))
    r = _paper_session(tmp_path, RiskLimits(**ALL_DAY), engine_config=ecfg)
    gap = replay_gap("p", root=tmp_path)
    assert r.fills > 0
    assert gap.matched_fills == gap.paper_fills == gap.replay_fills
    assert abs(gap.net_gap) < 1e-6 and gap.first_divergence_ns is None


def test_replay_refuses_a_report_without_tokens(tmp_path):
    _paper_session(tmp_path, RiskLimits(**ALL_DAY))
    path = tmp_path / "p.report.json"
    rep = json.loads(path.read_text())
    for bad in (None, []):
        rep["tokens"] = bad
        path.write_text(json.dumps(rep))
        with pytest.raises(ValueError, match="tokens"):
            replay_gap("p", root=tmp_path)


class RaiseOnTick(BaseStrategy):
    name = "raise-on-tick"

    def __init__(self, at=500):
        self.at = at
        self.n = 0

    def on_tick(self, ctx):
        self.n += 1
        if self.n == self.at:
            raise RuntimeError("boom")
        if ctx.position == 0 and not ctx.working_orders:
            ctx.buy(65)


def test_replay_trips_like_paper_when_the_strategy_raises(tmp_path):
    strat = RaiseOnTick(at=500)
    r = _paper_session(tmp_path, RiskLimits(**ALL_DAY), strat, f"{__name__}:RaiseOnTick")
    gap = replay_gap("p", root=tmp_path)
    assert strat.n == 500
    assert len(list(replay("p", root=tmp_path))) == 3000
    assert r.fills > 0
    assert gap.paper_tripped and gap.replay_tripped
    assert gap.ticks_replayed == gap.ticks_live == 499
    assert gap.matched_fills == gap.paper_fills == gap.replay_fills
    assert abs(gap.net_gap) < 1e-6


def test_compare_cli_reports_an_unbuildable_strategy_cleanly(tmp_path, monkeypatch, capsys):
    import shunkan.algo.__main__ as cli

    _paper_session(tmp_path, RiskLimits(**ALL_DAY))
    monkeypatch.setattr(cli, "TICKS_DIR", tmp_path)
    path = tmp_path / "p.report.json"
    rep = json.loads(path.read_text())
    for spec, params in (("no_such_module_xyz:S", {}), ("quote-both-sides", {"nope": 1})):
        rep["strategy_spec"], rep["strategy_params"] = spec, params
        path.write_text(json.dumps(rep))
        assert cli.main(["compare", "p"]) == 1
    err = capsys.readouterr().err
    assert "no_such_module_xyz" in err and "nope" in err
    assert not (tmp_path / "gaps.jsonl").exists()


def test_a_trip_does_not_end_the_paper_session(tmp_path, capsys):
    stamps = itertools.count(1_000_000_000_000, 2_000_000)
    strat = QuoteBothSides(edge=1.0, size=65, max_position=195)
    g = RiskGovernor(RiskLimits(max_daily_loss=300, max_drawdown=300, **ALL_DAY))
    pt = PaperTrader(strat, PaperConfig(tokens=[1], session="p", root=tmp_path, status_every=0.0,
                                        clock=lambda: next(stamps)), governor=g)
    feed = _ticks()

    def start(api_key, access_token):
        pt.recorder.start()
        pt._running = True

    def sleep(_):
        chunk = list(itertools.islice(feed, 100))
        if not chunk:
            raise KeyboardInterrupt
        for batch in chunk:
            pt.on_ticks(batch)

    pt.start, pt._sleep = start, sleep
    r = pt.run_until("key", "token", None)
    save_report(r, g, tmp_path / "p.report.json", strategy_spec="quote-both-sides",
                strategy_params=strategy_params(strat), tokens=[1], engine_config=pt.engine.cfg)
    gap = replay_gap("p", root=tmp_path)
    assert g.stats.tripped_at < (1_000_000_000_000 + 2_000_000 * 2900) / 1e9
    assert len(list(replay("p", root=tmp_path))) == 3000
    assert gap.ticks_live == gap.ticks_replayed == 3000
    assert gap.paper_tripped and gap.replay_tripped
    assert gap.matched_fills == gap.paper_fills == gap.replay_fills
    assert abs(gap.net_gap) < 1e-6 and gap.first_divergence_ns is None
    assert capsys.readouterr().out.count("GOVERNOR TRIPPED") == 1


# --------------------------------------------------------------------------
# while tripped: nothing rests, nothing is held
# --------------------------------------------------------------------------

def _book(bid, ask, ltp, volume):
    bids = [(bid, 650, 3)] if bid else []
    return parse_binary(build_frame([build_full_packet(
        1, ltp, volume=volume, bids=bids, asks=[(ask, 650, 3)])]))[0]


class RestThenKill(BaseStrategy):
    name = "rest-then-kill"

    def __init__(self, kill=""):
        self.kill = kill
        self.n = 0
        self.resting = None

    def on_tick(self, ctx):
        self.n += 1
        if self.n == 2:
            ctx.buy(65)
        if self.n == 3:
            self.resting = ctx.buy(65, limit=99.0)
        if self.n == 10:
            open(self.kill, "w").close()
            ctx.sell(65, limit=100.0)


def test_a_trip_cancels_resting_orders_and_keeps_the_book_flat(tmp_path):
    kill = tmp_path / "halt"
    g = RiskGovernor(RiskLimits(kill_file=kill, **ALL_DAY))
    strat = RestThenKill(kill=str(kill))
    e = Engine(BacktestConfig(session="t"), strat, g)
    tape = ([_book(100.0, 100.5, 100.25, 1000 + 100 * i) for i in range(9)]
            + [_book(None, 100.5, 100.25, 2000)]
            + [_book(98.0, 98.5, 98.25, 3000 + 100 * i) for i in range(20)])
    held = []
    for i, t in enumerate(tape):
        e.step(t, 1_000_000_000_000 + 100_000_000 * i)
        held.append(e.pos.quantity)
    r = e.finalize()
    assert g.tripped and held[8] == 65
    assert held[9] == 65 and all(q == 0 for q in held[10:])
    assert strat.resting.status is OrderStatus.CANCELLED and strat.resting.filled_qty == 0
    assert not e.fills.working and r.flattened_on_trip
    trip_ns = 1_000_000_000_000 + 100_000_000 * 9
    assert not [f for f in r.fill_log if f[0] >= trip_ns and f[5]]


def test_quote_both_sides_never_holds_after_the_trip_flattens():
    tripped = 0
    for seed in range(1, 41):
        g = RiskGovernor(RiskLimits(max_daily_loss=300, max_drawdown=300, **ALL_DAY))
        e = Engine(BacktestConfig(session="t"), QuoteBothSides(edge=1.0, size=65,
                                                               max_position=195), g)
        flat = False
        for i, batch in enumerate(_ticks(seed=seed)):
            e.step(batch[0], 1_000_000_000_000 + 2_000_000 * i)
            if g.tripped:
                assert not e.fills.working, seed
                flat = flat or e.pos.quantity == 0
                assert not (flat and e.pos.quantity), seed
        tripped += g.tripped
    assert tripped >= 30


def test_paper_cli_refuses_an_until_already_past(monkeypatch, capsys):
    import shunkan.algo.__main__ as cli

    monkeypatch.setattr(cli, "_seconds_until", lambda hhmm: 0.0)
    assert cli.main(["paper", "--tokens", "1", "--until", "09:00"]) == 1
    assert "already past" in capsys.readouterr().err


def _bad_touch(kind):
    if kind == "crossed":
        return _book(101.0, 100.5, 100.25, 2000)
    sane = _book(100.0, 100.5, 100.25, 2000)
    return dataclasses.replace(sane, bids=[DepthLevel(price=0.0, quantity=650, orders=3)])


@pytest.mark.parametrize("kind", ["crossed", "non-positive bid"])
def test_trip_flatten_waits_for_a_sane_touch(tmp_path, kind):
    kill = tmp_path / "halt"
    g = RiskGovernor(RiskLimits(kill_file=kill, **ALL_DAY))
    e = Engine(BacktestConfig(session="t"), RestThenKill(kill=str(kill)), g)
    tape = ([_book(100.0, 100.5, 100.25, 1000 + 100 * i) for i in range(9)]
            + [_bad_touch(kind)]
            + [_book(100.0, 100.5, 100.25, 3000 + 100 * i) for i in range(5)])
    held = []
    for i, t in enumerate(tape):
        e.step(t, 1_000_000_000_000 + 100_000_000 * i)
        held.append(e.pos.quantity)
    r = e.finalize()
    trip_ns = 1_000_000_000_000 + 100_000_000 * 9
    assert g.tripped and held[9] == 65
    assert not [f for f in r.fill_log if f[0] == trip_ns]
    assert all(q == 0 for q in held[10:]) and r.flattened_on_trip


class RestThenRaise(BaseStrategy):
    name = "rest-then-raise"

    def __init__(self):
        self.n = 0
        self.resting = None

    def on_tick(self, ctx):
        self.n += 1
        if self.n == 2:
            self.resting = ctx.buy(65, limit=99.0)
        if self.n == 10:
            raise RuntimeError("boom")


def test_a_trip_mid_batch_cancels_before_the_next_tick_matches(tmp_path):
    stamps = itertools.count(1_000_000_000_000, 100_000_000)
    strat = RestThenRaise()
    g = RiskGovernor(RiskLimits(**ALL_DAY))
    pt = PaperTrader(strat, PaperConfig(tokens=[1], session="p", root=tmp_path,
                                        clock=lambda: next(stamps)), governor=g)
    pt.recorder.start()
    pt._running = True
    for i in range(9):
        pt.on_ticks([_book(100.0, 100.5, 100.25, 1000 + 100 * i)])
    pt.on_ticks([_book(100.0, 100.5, 100.25, 2000), _book(98.0, 98.5, 98.25, 3000)])
    r = pt.stop()
    assert g.tripped and strat.n == 11
    assert strat.resting.status is OrderStatus.CANCELLED and strat.resting.filled_qty == 0
    assert r.fills == 0
    save_report(r, g, tmp_path / "p.report.json", strategy_spec=f"{__name__}:RestThenRaise",
                strategy_params=strategy_params(strat), tokens=[1], engine_config=pt.engine.cfg)
    gap = replay_gap("p", root=tmp_path)
    assert gap.replay_fills == 0 and gap.replay_tripped and abs(gap.net_gap) < 1e-6

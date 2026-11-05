"""Live-path guards for the paper CLI: limits, relaunch, stop race, chain, tokens, gap."""

import datetime
import itertools
import json
import math
import random
import threading

import pandas as pd
import pytest

import shunkan.algo.__main__ as cli
import shunkan.algo.paper as paper_mod
import shunkan.algo.recorder as recorder_mod
from shunkan.algo import (BaseStrategy, NullStrategy, PaperConfig, PaperTrader, QuoteBothSides,
                          RiskGovernor, RiskLimits, Segment, TickRecorder, limits_from_dict,
                          limits_to_dict, replay, save_report, strategy_params)
from shunkan.algo.chain import select_chain
from shunkan.algo.paper import Gap, append_gap, replay_gap
from shunkan.stream.parser import build_frame, build_full_packet, parse_binary

FUT = 111
NOON_NS = int(datetime.datetime(2026, 11, 5, 12, 0).timestamp() * 1e9)
ALL_DAY = dict(session_open=datetime.time(0, 0), session_close=datetime.time(23, 59))


def fut_tick(px, volume):
    return parse_binary(build_frame([build_full_packet(
        FUT, px, volume=volume, bids=[(px - 0.5, 650, 5), (px - 1.0, 1300, 9)],
        asks=[(px + 0.5, 650, 5), (px + 1.0, 1300, 9)])]))[0]


def fut_tape(n=600, seed=5):
    rng = random.Random(seed)
    px, vol = 25_500.0, 1_000_000
    for _ in range(n):
        px += rng.choice((-0.5, 0.0, 0.5))
        vol += rng.randint(0, 650)
        yield fut_tick(px, vol)


def paper_on_future(limits, n=600):
    stamps = itertools.count(NOON_NS, 50_000_000)
    g = RiskGovernor(limits)
    pt = PaperTrader(QuoteBothSides(), PaperConfig(tokens=[FUT], session="f", record=False,
                                                   segment=Segment.FUTURE,
                                                   clock=lambda: next(stamps)), governor=g)
    pt._running = True
    for t in fut_tape(n):
        pt.on_ticks([t])
    return pt.stop(), g


def limits_for(*flags):
    return cli._limits_from(cli._parser().parse_args(["paper", "--tokens", str(FUT), *flags]))


# --- 1: instrument-sized limits from the CLI ---------------------------------

def test_default_limits_refuse_every_order_on_a_nifty_future():
    r, g = paper_on_future(limits_for())
    assert g.stats.orders_rejected > 0 and r.orders == 0
    assert dict(g.stats.rejections) == {"GROSS_EXPOSURE": g.stats.orders_rejected}


def test_max_gross_exposure_flag_lets_future_orders_through():
    r, g = paper_on_future(limits_for("--max-gross-exposure", "6000000"))
    assert g.stats.orders_allowed > 0 and r.orders > 0
    assert "GROSS_EXPOSURE" not in g.stats.rejections


def test_new_limit_flags_reach_the_limits_and_survive_the_report():
    lim = limits_for("--max-gross-exposure", "6000000", "--max-orders-per-day", "50000",
                     "--max-orders-per-minute", "300", "--max-orders-per-second", "5")
    assert (lim.max_gross_exposure, lim.max_orders_per_day, lim.max_orders_per_minute,
            lim.max_orders_per_second) == (6_000_000.0, 50_000, 300, 5)
    assert limits_from_dict(json.loads(json.dumps(limits_to_dict(lim)))) == lim
    assert limits_for() == RiskLimits()
    assert RiskLimits().max_gross_exposure == 200_000.0


# --- the paper CLI, offline ---------------------------------------------------

class FakeTicker:
    built = []

    def __init__(self, *a, **k):
        FakeTicker.built.append(a)

    def start(self, *a, **k):
        raise AssertionError("connected")

    def stop(self):
        pass


def chain_frame():
    rows, tok = [], 5000
    for k in range(25_000, 26_001, 50):
        for right in ("CE", "PE"):
            tok += 1
            rows.append(dict(instrument_token=tok, name="NIFTY", instrument_type=right,
                             segment="NFO-OPT", expiry="2099-11-12", strike=float(k)))
    return pd.DataFrame(rows)


@pytest.fixture
def offline(monkeypatch, tmp_path):
    import shunkan.data.brokers as brokers
    import shunkan.data.kite_fno as kite_fno
    import shunkan.stream.ticker as ticker

    runs = []

    class Quote:
        price = 25_512.0

    class FakeKite:
        fail = None

        def __init__(self, *a):
            pass

        def quote(self, symbol):
            if FakeKite.fail:
                raise RuntimeError(FakeKite.fail)
            return Quote()

    def fake_run(self, api_key, access_token, seconds):
        runs.append(self)
        return self.engine.finalize()

    FakeTicker.built = []
    state = {"instruments": chain_frame}
    for mod in (cli, recorder_mod, paper_mod):
        monkeypatch.setattr(mod, "TICKS_DIR", tmp_path)
    monkeypatch.setattr(brokers, "load_credentials",
                        lambda: {"zerodha": {"api_key": "k", "access_token": "t"}})
    monkeypatch.setattr(brokers, "KiteProvider", FakeKite)
    monkeypatch.setattr(kite_fno, "load_instruments", lambda *a, **k: state["instruments"]())
    monkeypatch.setattr(ticker, "KiteTicker", FakeTicker)
    monkeypatch.setattr(cli, "_resolve", lambda syms: {s.strip().upper(): 222 for s in syms})
    monkeypatch.setattr(PaperTrader, "run_until", fake_run)
    return {"runs": runs, "kite": FakeKite, "state": state, "root": tmp_path}


def test_paper_cli_runs_offline_with_the_watch_chain(offline, capsys):
    assert cli.main(["paper", "--tokens", str(FUT), "--watch-chain", "2", "--session", "w"]) == 0
    pt, = offline["runs"]
    assert len(pt.cfg.watch) == 2 + 10 and FUT not in pt.cfg.watch
    assert "watching 12 token(s)" in capsys.readouterr().out
    assert (offline["root"] / "w.report.json").exists()


# --- 4: a chain failure degrades to no watch ----------------------------------

@pytest.mark.parametrize("break_it", ["instruments", "ltp", "centre zero", "centre nan"])
def test_paper_cli_trades_without_a_watch_when_the_chain_fails(offline, capsys, break_it):
    argv = ["paper", "--tokens", str(FUT), "--watch-chain", "10", "--session", "w"]
    if break_it == "instruments":
        offline["state"]["instruments"] = lambda: (_ for _ in ()).throw(RuntimeError("dump down"))
    elif break_it == "ltp":
        offline["kite"].fail = "ltp down"
    else:
        argv += ["--chain-center", "0" if break_it == "centre zero" else "nan"]
    assert cli.main(argv) == 0
    pt, = offline["runs"]
    assert pt.cfg.watch == [] and pt._subscribed == [FUT]
    err = capsys.readouterr().err
    assert "no watch" in err and "NIFTY chain" in err


@pytest.mark.parametrize("centre", [0.0, -24_000.0, math.nan, math.inf, None])
def test_select_chain_refuses_a_centre_that_is_not_a_price(centre):
    with pytest.raises(ValueError, match="centre"):
        select_chain(chain_frame(), centre, 2, datetime.date(2026, 11, 5))


# --- 5: one traded token -------------------------------------------------------

@pytest.mark.parametrize("argv", [["--tokens", "111,333"], ["--tokens", "111", "--symbols", "X"]])
def test_paper_cli_refuses_more_than_one_traded_token(offline, capsys, argv):
    assert cli.main(["paper", *argv, "--session", "two"]) == 1
    assert offline["runs"] == [] and FakeTicker.built == []
    assert "one traded token" in capsys.readouterr().err


def test_paper_cli_accepts_a_repeated_token(offline):
    assert cli.main(["paper", "--tokens", "111,111", "--session", "one"]) == 0
    pt, = offline["runs"]
    assert pt.cfg.tokens == [FUT]


# --- 2: a same-name relaunch never touches the old tape -----------------------

def one_tick_session(root, session):
    rec = TickRecorder(tokens=[FUT], root=root, session=session)
    rec.start()
    rec.on_ticks([fut_tick(25_500.0, 1)])
    rec.stop()


def test_recorder_refuses_a_session_with_parts_or_metadata(tmp_path):
    one_tick_session(tmp_path, "s")
    parts = {p: p.read_bytes() for p in (tmp_path / "s").glob("*.parquet")}
    assert parts
    again = TickRecorder(tokens=[FUT], root=tmp_path, session="s")
    with pytest.raises(FileExistsError, match="'s'"):
        again.start()
    assert again._thread is None
    (tmp_path / "s.json").unlink()
    with pytest.raises(FileExistsError):
        TickRecorder(root=tmp_path, session="s").start()
    (tmp_path / "m.json").write_text("{}")
    with pytest.raises(FileExistsError):
        TickRecorder(root=tmp_path, session="m").start()
    assert {p: p.read_bytes() for p in (tmp_path / "s").glob("*.parquet")} == parts
    (tmp_path / "e").mkdir()
    fresh = TickRecorder(root=tmp_path, session="e")
    fresh.start()
    fresh.stop()


def test_paper_cli_refuses_a_taken_session_before_connecting(offline, capsys):
    one_tick_session(offline["root"], "taken")
    before = sorted(p.read_bytes() for p in (offline["root"] / "taken").iterdir())
    assert cli.main(["paper", "--tokens", str(FUT), "--session", "taken"]) == 1
    assert offline["runs"] == [] and FakeTicker.built == []
    assert "taken" in capsys.readouterr().err
    assert sorted(p.read_bytes() for p in (offline["root"] / "taken").iterdir()) == before
    assert not (offline["root"] / "taken.report.json").exists()


def test_record_cli_refuses_a_taken_session_before_connecting(offline, capsys):
    one_tick_session(offline["root"], "taken")
    meta = (offline["root"] / "taken.json").read_text()
    assert cli.main(["record", "--tokens", str(FUT), "--session", "taken"]) == 1
    assert FakeTicker.built == []
    assert "taken" in capsys.readouterr().err
    assert (offline["root"] / "taken.json").read_text() == meta


# --- 3: nothing lands after stop() returns ------------------------------------

class Counting(BaseStrategy):
    name = "counting"

    def __init__(self):
        self.n = 0

    def on_tick(self, ctx):
        self.n += 1


def test_stop_waits_for_an_in_flight_batch_and_nothing_lands_after(tmp_path):
    entered, release = threading.Event(), threading.Event()
    stamps = itertools.count(NOON_NS, 50_000_000)
    calls = itertools.count(1)

    def clock():
        if next(calls) == 3:
            entered.set()
            release.wait(5)
        return next(stamps)

    strat = Counting()
    pt = PaperTrader(strat, PaperConfig(tokens=[FUT], session="race", root=tmp_path,
                                        clock=clock), governor=RiskGovernor(RiskLimits()))
    pt.recorder.start()
    pt._running = True
    tape = fut_tape(10)
    pt.on_ticks([next(tape)])
    pt.on_ticks([next(tape)])
    inflight = threading.Thread(target=pt.on_ticks, args=([next(tape)],))
    inflight.start()
    assert entered.wait(5)
    at_stop = {}

    def stopper():
        r = pt.stop()
        at_stop.update(recorded=pt.recorder.stats.ticks_received, stepped=strat.n,
                       ticks=r.ticks)

    s = threading.Thread(target=stopper)
    s.start()
    s.join(0.3)
    release.set()
    inflight.join(5)
    s.join(5)
    assert not inflight.is_alive() and not s.is_alive()
    after = threading.Thread(target=pt.on_ticks, args=([next(tape)],))
    after.start()
    after.join(5)
    assert pt.recorder.stats.ticks_received == at_stop["recorded"]
    assert strat.n == at_stop["stepped"] == at_stop["ticks"]
    assert pt.recorder.stats.ticks_written == pt.recorder.stats.ticks_received
    assert len(list(replay("race", root=tmp_path))) == at_stop["recorded"]


# --- 6: no fills on either side is not a 0% match -----------------------------

def gap(paper_fills, replay_fills, matched):
    return Gap(session="g", paper_net=0.0, replay_net=0.0, paper_fills=paper_fills,
               replay_fills=replay_fills, matched_fills=matched, first_divergence_ns=None,
               paper_maker_ratio=0.0, replay_maker_ratio=0.0, ticks_live=10,
               ticks_replayed=10, ticks_dropped=0, paper_tripped=False, replay_tripped=False)


def test_match_rate_is_none_when_neither_side_filled():
    g = gap(0, 0, 0)
    assert g.match_rate is None
    assert "matched fills     0 (n/a)" in g.summary()
    assert json.loads(json.dumps(g.as_dict()))["match_rate"] is None
    assert gap(4, 2, 2).match_rate == 0.5 and gap(0, 3, 0).match_rate == 0.0


def test_a_null_session_logs_a_null_match_rate(tmp_path):
    stamps = itertools.count(NOON_NS, 50_000_000)
    g = RiskGovernor(RiskLimits(**ALL_DAY))
    strat = NullStrategy()
    pt = PaperTrader(strat, PaperConfig(tokens=[FUT], session="n", root=tmp_path,
                                        clock=lambda: next(stamps)), governor=g)
    pt.recorder.start()
    pt._running = True
    for t in fut_tape(50):
        pt.on_ticks([t])
    r = pt.stop()
    save_report(r, g, tmp_path / "n.report.json", strategy_spec="null",
                strategy_params=strategy_params(strat), tokens=[FUT], engine_config=pt.engine.cfg)
    result = replay_gap("n", root=tmp_path)
    assert result.match_rate is None
    append_gap(result, tmp_path / "gaps.jsonl")
    assert json.loads((tmp_path / "gaps.jsonl").read_text())["match_rate"] is None


def test_the_live_runner_argv_runs_offline_with_future_sized_limits(offline, monkeypatch):
    monkeypatch.setattr(cli, "_seconds_until", lambda hhmm: 3600.0)
    argv = ["paper", "--symbols", "NIFTY26NOVFUT", "--strategy", "quote-both-sides",
            "--watch-chain", "10", "--session", "2026-11-05-paper", "--until", "15:30",
            "--max-gross-exposure", "6000000", "--max-orders-per-day", "50000",
            "--max-loss", "50000", "--max-drawdown", "75000"]
    assert cli.main(argv) == 0
    pt, = offline["runs"]
    assert pt.cfg.tokens == [222] and pt.cfg.segment is Segment.FUTURE and len(pt.cfg.watch) == 44
    rep = json.loads((offline["root"] / "2026-11-05-paper.report.json").read_text())
    lim = limits_from_dict(rep["limits"])
    assert (lim.max_gross_exposure, lim.max_orders_per_day, lim.max_daily_loss,
            lim.max_drawdown) == (6_000_000.0, 50_000, 50_000.0, 75_000.0)
    assert rep["segment"] == "future" and rep["tokens"] == [222]

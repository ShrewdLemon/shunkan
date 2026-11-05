import datetime
import itertools
import json
from pathlib import Path

import pytest

from shunkan.algo import (BaseStrategy, PaperConfig, PaperTrader, QuoteBothSides, RiskGovernor,
                          RiskLimits, Segment, build_strategy, limits_from_dict, limits_to_dict,
                          replay, replay_gap, save_report, strategy_params)
from shunkan.stream.parser import build_frame, build_full_packet, parse_binary


def tick(token=1, ltp=158.25, volume=0):
    pkt = build_full_packet(token, ltp, volume=volume,
                            bids=[(158.10, 65, 1)], asks=[(159.60, 130, 1)])
    return parse_binary(build_frame([pkt]))[0]


def test_limits_round_trip_through_json():
    lim = RiskLimits(max_daily_loss=1234.5, session_close=datetime.time(15, 20), kill_file=None)
    back = limits_from_dict(json.loads(json.dumps(limits_to_dict(lim))))
    assert back == lim


def test_build_strategy_applies_params():
    s = build_strategy("quote-both-sides", {"edge": 2.5, "size": 65, "max_position": 130})
    assert isinstance(s, QuoteBothSides) and s.edge == 2.5 and s.max_position == 130
    assert strategy_params(s) == {"edge": 2.5, "size": 65, "max_position": 130}
    with pytest.raises(SystemExit):
        build_strategy("nope")


def test_watch_tokens_are_recorded_not_traded(tmp_path):
    seen = []

    class Spy(BaseStrategy):
        name = "spy"

        def on_tick(self, ctx):
            seen.append(ctx.tick.token)

    pt = PaperTrader(Spy(), PaperConfig(tokens=[1], watch=[2, 3], session="p", root=tmp_path),
                     governor=RiskGovernor(RiskLimits()))
    pt.recorder.start()
    pt._running = True
    for i in range(10):
        pt.on_ticks([tick(1, volume=i), tick(2, volume=i), tick(3, volume=i)])
    r = pt.stop()
    assert set(seen) == {1}
    assert r.ticks == 10 and pt.status["ticks"] == 10
    assert pt.status["recorded"] == 30 and "tape" in pt.status_line()
    assert len(list(replay("p", root=tmp_path))) == 30
    assert len(list(replay("p", root=tmp_path, tokens=[1]))) == 10


class BuyOnce(BaseStrategy):
    name = "buy-once"

    def __init__(self):
        self.done = False

    def on_tick(self, ctx):
        if not self.done:
            ctx.buy(65)
            self.done = True


def test_report_carries_everything_needed_to_replay(tmp_path):
    g = RiskGovernor(RiskLimits(session_open=datetime.time(0, 0),
                                session_close=datetime.time(23, 59)))
    stamps = itertools.count(1_000_000_000_000, 20_000_000)
    pt = PaperTrader(BuyOnce(), PaperConfig(tokens=[1], session="p", root=tmp_path,
                                            segment=Segment.FUTURE, clock=lambda: next(stamps)),
                     governor=g)
    pt.recorder.start()
    pt._running = True
    for i in range(40):
        pt.on_ticks([tick(1, ltp=159.60, volume=1000 + 500 * i)])
    r = pt.stop()
    path = tmp_path / "p.report.json"
    save_report(r, g, path, strategy_spec=f"{__name__}:BuyOnce",
                strategy_params=strategy_params(BuyOnce()), tokens=[1], watch=[2],
                segment="future", engine_config=pt.engine.cfg)
    rep = json.loads(path.read_text())
    for k in ("limits", "strategy_spec", "strategy_params", "tokens", "watch", "segment", "fill_log"):
        assert k in rep
    assert rep["segment"] == "future"
    assert len(rep["fill_log"]) == r.fills
    assert r.fills >= 1
    assert pt.engine.cfg.segment is Segment.FUTURE
    gap = replay_gap("p", root=tmp_path)
    assert gap.net_gap == 0 and gap.first_divergence_ns is None
    assert gap.matched_fills == gap.replay_fills == gap.paper_fills == r.fills


class EveryN(BaseStrategy):
    name = "every-n"

    def __init__(self, every=100, size=65):
        self.every = every
        self.size = size
        self.n = 0
        self.done = False

    def on_tick(self, ctx):
        self.n += 1


class PrivateArg(BaseStrategy):
    def __init__(self, edge=1.0):
        self._edge = edge

    def on_tick(self, ctx):
        pass


class Hidden(BaseStrategy):
    def __init__(self, edge):
        self._e = edge

    def on_tick(self, ctx):
        pass


class NotJson(BaseStrategy):
    def __init__(self, where=None):
        self.where = where

    def on_tick(self, ctx):
        pass


def test_strategy_params_are_constructor_arguments_not_state():
    s = EveryN(every=7)
    s.n, s.done = 999, True
    assert strategy_params(s) == {"every": 7, "size": 65}
    back = build_strategy(f"{__name__}:EveryN", strategy_params(s))
    assert (back.every, back.size, back.n) == (7, 65, 0)
    assert strategy_params(PrivateArg(edge=2.5)) == {"edge": 2.5}
    assert strategy_params(BuyOnce()) == {}


def test_strategy_params_refuse_what_cannot_be_rebuilt():
    with pytest.raises(ValueError, match="edge"):
        strategy_params(Hidden(2.5))
    with pytest.raises(ValueError, match="where"):
        strategy_params(NotJson(where=Path("/tmp")))


class Sized(BaseStrategy):
    def __init__(self, size=65):
        self.size = size

    def on_tick(self, ctx):
        pass


class Forwards(Sized):
    def __init__(self, edge=1.0, **kw):
        super().__init__(**kw)
        self.edge = edge
        self.n = 0


class Open(BaseStrategy):
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def on_tick(self, ctx):
        pass


def test_strategy_params_check_the_strategy_rebuilds():
    with pytest.raises(ValueError, match="Forwards"):
        strategy_params(Forwards())
    assert strategy_params(Open(edge=2.0)) == {"edge": 2.0}

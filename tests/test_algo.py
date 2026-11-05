"""Tests for the tick-level research harness.

The point of most of these is to pin down the *pessimism* of the fill model.
An execution simulator that drifts optimistic produces backtests that look
better every release, which is the worst possible failure mode: nothing
breaks, the numbers just quietly stop being true.
"""

from __future__ import annotations

import itertools
import random
from datetime import date

import pytest

from shunkan.algo import (
    DEFAULT_RATES,
    RATES_UNTIL_MARCH_2026,
    BacktestConfig,
    BaseStrategy,
    FillConfig,
    FillEngine,
    NullStrategy,
    Position,
    Segment,
    Side,
    TickRecorder,
    backtest,
    breakeven_points,
    charge_leg,
    charge_round_trip,
    exercise_stt,
    imbalance,
    is_tradeable,
    rates_on,
    replay,
    session_meta,
    slippage,
    sweep_buy,
    sweep_sell,
)
from shunkan.stream.parser import build_frame, build_full_packet, build_quote_packet, parse_binary

BIDS = [(158.10, 65, 1), (158.00, 1105, 3), (157.75, 65, 1)]
ASKS = [(159.60, 130, 1), (159.65, 195, 1), (159.70, 195, 1)]


def tick(ltp=158.25, volume=0, bids=BIDS, asks=ASKS, token=1, oi=0):
    pkt = build_full_packet(token, ltp, volume=volume, oi=oi, bids=bids, asks=asks)
    return parse_binary(build_frame([pkt]))[0]


# --------------------------------------------------------------------------
# parser: depth
# --------------------------------------------------------------------------

def test_full_packet_is_184_bytes_and_round_trips():
    pkt = build_full_packet(1, 158.25, bids=BIDS, asks=ASKS)
    assert len(pkt) == 184
    t = parse_binary(build_frame([pkt]))[0]
    assert t.mode == "full"
    assert t.bid == 158.10 and t.ask == 159.60
    assert t.spread == pytest.approx(1.50)
    assert t.mid == pytest.approx(158.85)
    assert len(t.bids) == 3 and len(t.asks) == 3


def test_quote_mode_still_parses_and_carries_no_depth():
    """Back-compat: existing callers pass quote packets and must be unaffected."""
    t = parse_binary(build_frame([build_quote_packet(1, 100.0, volume=5)]))[0]
    assert t.ltp == 100.0 and t.volume == 5
    assert t.bids == [] and t.asks == []
    assert t.spread == 0.0 and t.mid == 100.0  # falls back to LTP


def test_short_book_does_not_invent_zero_priced_levels():
    t = tick(bids=[(49.9, 10, 1)], asks=[(50.1, 20, 2)])
    assert len(t.bids) == 1 and len(t.asks) == 1
    assert all(l.price > 0 for l in t.bids + t.asks)


def test_microprice_leans_toward_the_thin_side():
    t = tick()  # 65 on the bid, 130 on the offer
    assert t.microprice < t.mid  # heavier offer pulls fair value down


# --------------------------------------------------------------------------
# charges
# --------------------------------------------------------------------------

def test_charges_match_a_real_contract_note():
    """Measured against a real NIFTY option round trip: 1950 @ 69.20 -> 72.00, Rs 305.79, before April 2026."""
    c = charge_round_trip(69.20, 72.00, 1950, rates=RATES_UNTIL_MARCH_2026)
    assert c.brokerage == pytest.approx(40.0)          # Rs 20 x 2 orders
    assert c.stt == pytest.approx(140.40)              # 0.1% of sell premium
    assert c.total == pytest.approx(305.79, abs=2.0)


def test_rates_follow_the_session_date():
    assert rates_on(date(2026, 3, 31)) is RATES_UNTIL_MARCH_2026
    assert rates_on(date(2026, 4, 1)) is DEFAULT_RATES


def test_april_2026_stt_hike():
    """NSE's STT table, from 1 April 2026: option sale 0.10% -> 0.15%, exercise 0.125% -> 0.15%, futures 0.02% -> 0.05%."""
    old, new = RATES_UNTIL_MARCH_2026, DEFAULT_RATES
    assert charge_leg(100.0, 65, False, rates=old).stt == pytest.approx(6.50)
    assert charge_leg(100.0, 65, False, rates=new).stt == pytest.approx(9.75)
    assert charge_leg(100.0, 65, False, Segment.FUTURE, old).stt == pytest.approx(1.30)
    assert charge_leg(100.0, 65, False, Segment.FUTURE, new).stt == pytest.approx(3.25)
    assert exercise_stt(100.0, 65, old) == pytest.approx(8.125)
    assert exercise_stt(100.0, 65, new) == pytest.approx(9.75)


def test_exchange_outflow_per_crore_is_unchanged_by_the_march_2026_ipft_move():
    """NSE/FA/73061: options Rs 3,503 + Rs 50 IPFT became Rs 3,552.99 + Rs 0.01 per crore; futures Rs 173 + 10 became 182.99 + 0.01."""
    for r in (RATES_UNTIL_MARCH_2026, DEFAULT_RATES):
        assert (r.exch_option + r.ipft_option) * 1e7 == pytest.approx(3553)
        assert (r.exch_future + r.ipft_future) * 1e7 == pytest.approx(183)


def test_stt_is_sell_side_only():
    assert charge_leg(100.0, 65, is_buy=True).stt == 0.0
    assert charge_leg(100.0, 65, is_buy=False).stt > 0.0


def test_crossing_the_spread_dwarfs_the_taxes():
    """The central economic fact this harness exists to expose. The April 2026 STT rise narrowed it from 5.7x to 4.7x."""
    passive = breakeven_points(158.85, 1950, spread=0.0)
    crossing = breakeven_points(158.85, 1950, spread=1.50)
    assert passive == pytest.approx(0.401, abs=0.01)
    assert crossing == pytest.approx(1.901, abs=0.01)
    assert crossing > passive * 4.5
    assert breakeven_points(158.85, 1950, spread=0.0, rates=RATES_UNTIL_MARCH_2026) == pytest.approx(0.321, abs=0.01)


def test_index_option_expiry_is_cheaper_than_squaring_off():
    """Documents the direction of a trade-off that folklore gets backwards.

    Cash-settled index options only. Stock options are physically settled and
    the calculus is entirely different -- see `exercise_stt`.
    """
    for intrinsic in (5, 20, 100, 300):
        assert exercise_stt(intrinsic, 1950) < charge_leg(intrinsic, 1950, False).total


def test_futures_use_notional_rates_not_option_rates():
    opt = charge_leg(100.0, 65, False, Segment.OPTION)
    fut = charge_leg(100.0, 65, False, Segment.FUTURE)
    assert fut.stt < opt.stt and fut.exchange < opt.exchange


# --------------------------------------------------------------------------
# book
# --------------------------------------------------------------------------

def test_market_order_is_capped_by_visible_liquidity():
    t = tick()
    s = sweep_buy(t, 10_000)
    assert s.filled == 130 + 195 + 195
    assert s.exhausted is True


def test_sweeping_deeper_costs_more_than_the_touch():
    t = tick()
    assert slippage(t, 65, is_buy=True) < slippage(t, 500, is_buy=True)


def test_crossed_book_is_not_tradeable():
    assert not is_tradeable(tick(bids=[(160.0, 65, 1)], asks=[(159.0, 65, 1)]))
    assert not is_tradeable(tick(bids=[(158.1, 65, 1)], asks=[]))
    assert is_tradeable(tick())


def test_imbalance_sign():
    heavy_bid = tick(bids=[(158.1, 1000, 1)], asks=[(159.6, 10, 1)])
    assert imbalance(heavy_bid) > 0


# --------------------------------------------------------------------------
# fill model — one test per pessimism rule
# --------------------------------------------------------------------------

def test_rule1_no_same_tick_fill():
    e = FillEngine(FillConfig(latency_ns=50_000_000))
    e.place(1, Side.BUY, 65, ts_ns=1000)
    assert e.on_tick(tick(volume=100), 1000) == []          # too soon
    assert len(e.on_tick(tick(volume=200), 10**9)) == 1      # aged past latency


def test_rule2_resting_order_needs_trade_evidence():
    e = FillEngine(FillConfig(latency_ns=0))
    e.place(1, Side.BUY, 65, ts_ns=0, limit_price=157.00)  # below the book
    e.on_tick(tick(volume=1000), 1)
    assert e.on_tick(tick(ltp=158.25, volume=2000), 2) == []  # never traded at 157


def test_rule3_queue_position_must_drain_first():
    e = FillEngine(FillConfig(latency_ns=0, participation=0.5))
    o = e.place(1, Side.BUY, 65, ts_ns=0, limit_price=158.10)
    e.on_tick(tick(volume=1000), 1)
    assert o.queue_ahead == 65                                # 65 already resting
    e.on_tick(tick(ltp=158.10, volume=1100), 2)               # 50 ours -> queue 15
    assert o.filled_qty == 0 and o.queue_ahead == 15
    e.on_tick(tick(ltp=158.10, volume=1200), 3)               # clears, then fills
    assert o.filled_qty > 0


def test_rule4_cannot_fill_more_than_actually_traded():
    e = FillEngine(FillConfig(latency_ns=0))
    o = e.place(1, Side.BUY, 1000, ts_ns=0, limit_price=158.10)
    e.on_tick(tick(volume=5000), 1)
    o.queue_ahead = 0
    fills = e.on_tick(tick(ltp=158.00, volume=5020), 2)  # traded through by 20
    assert fills[0].quantity == 20


def test_rule5_partial_fill_when_book_is_thin():
    e = FillEngine(FillConfig(latency_ns=0))
    o = e.place(1, Side.BUY, 10_000, ts_ns=0)
    e.on_tick(tick(volume=1), 1)
    assert 0 < o.filled_qty < 10_000


def test_rule6_no_fills_on_a_broken_book():
    e = FillEngine(FillConfig(latency_ns=0))
    e.place(1, Side.BUY, 65, ts_ns=0)
    crossed = tick(bids=[(160.0, 65, 1)], asks=[(159.0, 65, 1)])
    assert e.on_tick(crossed, 1) == []


def test_limit_order_never_fills_worse_than_its_limit():
    e = FillEngine(FillConfig(latency_ns=0))
    o = e.place(1, Side.BUY, 400, ts_ns=0, limit_price=159.65)
    e.on_tick(tick(volume=1), 1)
    assert o.avg_price <= 159.65


def test_volume_going_backwards_claims_no_trades():
    """Feed resets happen. Treating a reset as huge volume would fill everything."""
    e = FillEngine(FillConfig(latency_ns=0))
    o = e.place(1, Side.BUY, 65, ts_ns=0, limit_price=158.10)
    e.on_tick(tick(volume=10_000), 1)
    o.queue_ahead = 0
    assert e.on_tick(tick(ltp=158.00, volume=5), 2) == []


# --------------------------------------------------------------------------
# position accounting
# --------------------------------------------------------------------------

def test_average_cost_and_realized_pnl():
    p = Position()
    p.apply(100.0, +65, 0)
    p.apply(102.0, +65, 1)
    assert p.avg_price == pytest.approx(101.0)
    trip = p.apply(105.0, -65, 2)
    assert trip is not None
    assert trip.gross == pytest.approx((105.0 - 101.0) * 65)
    assert p.quantity == 65


def test_position_flipping_through_zero_resets_basis():
    p = Position()
    p.apply(100.0, +65, 0)
    trip = p.apply(110.0, -130, 1)          # close 65 long, open 65 short
    assert trip.gross == pytest.approx(10.0 * 65)
    assert p.quantity == -65
    assert p.avg_price == pytest.approx(110.0)


def test_short_then_cover_is_profitable_when_price_falls():
    p = Position()
    p.apply(100.0, -65, 0)
    trip = p.apply(90.0, +65, 1)
    assert trip.gross == pytest.approx(10.0 * 65)
    assert p.quantity == 0


# --------------------------------------------------------------------------
# recorder / replay / engine
# --------------------------------------------------------------------------

def _write_session(tmp_path, n=2000, seed=7, spacing_ns=2_000_000):
    clk = itertools.count(1_000_000_000_000, spacing_ns)
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
            rec.on_ticks(
                parse_binary(build_frame([build_full_packet(
                    1, ltp, volume=vol,
                    bids=[(b, 65, 1), (round(b - 0.05, 2), 1105, 3)],
                    asks=[(a, 130, 1), (round(a + 0.05, 2), 195, 1)],
                )]))
            )
    return rec.stats


def test_recorder_loses_nothing_and_replay_round_trips(tmp_path):
    stats = _write_session(tmp_path, n=1000)
    assert stats.ticks_received == 1000
    assert stats.ticks_written == 1000
    assert stats.ticks_dropped == 0
    events = list(replay("s", root=tmp_path))
    assert len(events) == 1000
    assert all(events[i].ts_ns <= events[i + 1].ts_ns for i in range(len(events) - 1))
    t = events[0].tick
    assert t.bid > 0 and t.ask > t.bid and len(t.bids) == 2


def test_metadata_lives_outside_the_parquet_directory(tmp_path):
    """So that pq.read_table(session_dir) — the obvious call — works."""
    import pyarrow.parquet as pq

    _write_session(tmp_path, n=200)
    assert {p.suffix for p in (tmp_path / "s").iterdir()} == {".parquet"}
    assert pq.read_table(tmp_path / "s").num_rows == 200
    assert session_meta("s", root=tmp_path)["ticks_written"] == 200


def test_token_filter_and_time_window(tmp_path):
    _write_session(tmp_path, n=500)
    assert list(replay("s", root=tmp_path, tokens=[999])) == []
    all_ev = list(replay("s", root=tmp_path))
    half = all_ev[len(all_ev) // 2].ts_ns
    assert len(list(replay("s", root=tmp_path, start_ns=half))) < len(all_ev)


def test_null_strategy_is_exactly_zero(tmp_path):
    """The engine's control. Any drift here invalidates every other result."""
    _write_session(tmp_path, n=1000)
    r = backtest(BacktestConfig(session="s", root=tmp_path, tokens=[1]), NullStrategy())
    assert r.ticks == 1000
    assert r.orders == 0 and r.fills == 0
    assert r.net_pnl == 0.0
    assert r.charges.total == 0.0
    assert r.round_trips == []


def test_a_resting_order_eventually_fills(tmp_path):
    """Guards the fixture bug where ticks share a timestamp and never age."""
    _write_session(tmp_path, n=2000)

    class PostAndLeave(BaseStrategy):
        name = "post-and-leave"

        def __init__(self):
            self.placed = False

        def on_tick(self, ctx):
            if not self.placed and ctx.tick.bids:
                ctx.buy(65, limit=ctx.tick.bid)
                self.placed = True

    r = backtest(BacktestConfig(session="s", root=tmp_path, tokens=[1]), PostAndLeave())
    assert r.fills >= 1
    assert r.maker_fills >= 1


def test_every_fill_is_charged(tmp_path):
    _write_session(tmp_path, n=2000)

    class Taker(BaseStrategy):
        name = "taker"

        def __init__(self):
            self.n = 0

        def on_tick(self, ctx):
            self.n += 1
            if self.n % 400 == 0:
                ctx.buy(65) if (self.n // 400) % 2 else ctx.sell(65)

    r = backtest(BacktestConfig(session="s", root=tmp_path, tokens=[1]), Taker())
    assert r.fills > 0
    assert r.charges.total > 0
    assert r.net_pnl == pytest.approx(r.gross_pnl - r.charges.total)


def test_open_position_is_flattened_not_hidden(tmp_path):
    """A strategy left long at the close must pay to get out, not book the
    mark as if it were free."""
    _write_session(tmp_path, n=1000)

    class BuyOnce(BaseStrategy):
        name = "buy-once"

        def __init__(self):
            self.done = False

        def on_tick(self, ctx):
            if not self.done:
                ctx.buy(65)
                self.done = True

    cfg = BacktestConfig(session="s", root=tmp_path, tokens=[1])
    r = backtest(cfg, BuyOnce())
    assert r.open_position == 0          # flattened
    assert len(r.round_trips) == 1
    assert r.charges.total > 0

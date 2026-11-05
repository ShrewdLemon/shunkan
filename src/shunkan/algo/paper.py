"""Paper trading: live feed, real book, simulated fills, no broker orders.

This module's static imports include nothing that can place an order: no
`KiteProvider`, no `place_order`, no REST endpoint that mutates an account.
The only broker surface it touches is the read-only market-data socket. The
AST test enforces this. It covers static imports only: `replay_gap` imports
the strategy a report names, dynamically, and that code is not checked here.

What paper trading is FOR, precisely:

  A backtest tells you how a strategy behaves against your fill assumptions.
  Paper runs the same engine and the same fill model against the live tape.
  `replay_gap` replays the recording with the report's own strategy, limits
  and tokens. Any gap comes from timing, dropped ticks or setup: it measures
  determinism, not whether the fill model is true. Only live orders test that.

So a paper run records while it trades, and its report carries everything
needed to rebuild the run.

Run it for weeks, not days, and across regimes: trending, chopping, expiry,
and the 15:15-15:40 auction window where the cash reference freezes.
"""

from __future__ import annotations

import bisect
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from shunkan.algo.charges import Rates, Segment
from shunkan.algo.engine import BacktestConfig, Engine
from shunkan.algo.fills import FillConfig
from shunkan.algo.metrics import Result
from shunkan.algo.recorder import TICKS_DIR, TickRecorder
from shunkan.algo.replay import replay, session_meta
from shunkan.algo.risk import (
    Reason,
    RiskGovernor,
    RiskLimits,
    limits_from_dict,
    limits_to_dict,
)
from shunkan.algo.strategy import build_strategy
from shunkan.stream.parser import Tick


@dataclass
class PaperConfig:
    tokens: list[int]
    session: str | None = None          # recording name; defaults to today
    root: Path | None = None
    record: bool = True                 # capture the tape while trading
    status_every: float = 10.0          # seconds between status lines
    watch: list[int] = field(default_factory=list)  # recorded, never traded
    segment: Segment = Segment.OPTION
    clock: Callable[[], int] | None = None  # ns batch stamp; time.time_ns


class PaperTrader:
    """Drive a strategy from the live socket through the backtest engine.

        pt = PaperTrader(strategy, PaperConfig(tokens=[...]), governor)
        pt.start(api_key, access_token)
        ...
        result = pt.stop()

    Ticks arrive on the socket thread. `Engine.step` is not thread-safe, so
    everything is serialised behind one lock; at NIFTY option tick rates this
    is not close to a bottleneck and the alternative is a race in P&L
    accounting that would be nearly impossible to reproduce.
    """

    def __init__(
        self,
        strategy,
        config: PaperConfig,
        governor: RiskGovernor | None = None,
        engine_config: BacktestConfig | None = None,
    ) -> None:
        self.cfg = config
        self.session = config.session or time.strftime("%Y-%m-%d-paper")
        self.governor = governor or RiskGovernor(RiskLimits())
        ecfg = engine_config or BacktestConfig(session=self.session, root=config.root)
        ecfg.session = self.session
        ecfg.root = config.root
        ecfg.tokens = config.tokens
        ecfg.segment = config.segment
        self.engine = Engine(ecfg, strategy, self.governor)
        self._traded = set(config.tokens)
        self._subscribed = sorted(self._traded | set(config.watch))
        self.recorder = (
            TickRecorder(tokens=self._subscribed, root=config.root, session=self.session)
            if config.record
            else None
        )
        self._clock = config.clock or time.time_ns
        self._sleep = time.sleep
        self._lock = threading.Lock()
        self._ticker = None
        self._running = False
        self._halted = False            # strategy raised: keep recording, stop stepping
        self._started_at = 0.0
        self._last_status = 0.0
        self._ticks = 0

    # ---- lifecycle ------------------------------------------------------

    def start(self, api_key: str, access_token: str, ticker=None) -> None:
        """`ticker` replaces the Kite socket: anything with `.start(tokens, callback,
        mode="full")` and `.stop()`."""
        if self.recorder is not None:
            self.recorder.start()
        self.engine.result.session = self.session
        if ticker is None:
            from shunkan.stream.ticker import KiteTicker

            ticker = KiteTicker(api_key, access_token)
        self._ticker = ticker
        self._running = True
        self._started_at = time.time()
        self._ticker.start(self._subscribed, self.on_ticks, mode="full")

    def on_ticks(self, ticks: list[Tick]) -> None:
        """Socket-thread callback. Records first, then trades, so a strategy
        exception can never cost us the tape. Watch tokens are only recorded.
        One stamp per batch, shared with the recorder, so replay sees paper's clock.
        All under the lock, so nothing is recorded or stepped once `stop` holds it."""
        if not ticks:
            return
        with self._lock:
            if not self._running:
                return
            ts = self._clock()
            if self.recorder is not None:
                self.recorder.on_ticks(ticks, ts_ns=ts)
            if self._halted:
                return
            for t in ticks:
                if t.token not in self._traded:
                    continue
                self._ticks += 1
                try:
                    self.engine.step(t, ts)
                except Exception as exc:  # noqa: BLE001
                    # A broken strategy must not take down the session or the
                    # recording. Trip the governor and stop stepping after this
                    # batch (replay_gap does the same); the tape keeps running.
                    self.governor.trip(
                        Reason.MANUAL,
                        f"strategy raised {type(exc).__name__}: {exc}",
                    )
                    self._halted = True

    def stop(self) -> Result:
        with self._lock:                # waits out a batch in flight
            self._running = False
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None
        with self._lock:
            result = self.engine.finalize()
        if self.recorder is not None:
            self.recorder.stop()
        return result

    # ---- monitoring ------------------------------------------------------

    @property
    def status(self) -> dict:
        e = self.engine
        eq = e.result.equity_curve[-1][1] if e.result.equity_curve else 0.0
        return {
            "elapsed": round(time.time() - self._started_at, 1),
            "ticks": self._ticks,
            "recorded": self.recorder.stats.ticks_received if self.recorder else None,
            "position": e.pos.quantity,
            "equity": round(eq, 2),
            "orders": e.result.orders,
            "rejected": e.result.orders_rejected,
            "fills": e.result.fills,
            "state": self.governor.state.value,
        }

    def status_line(self) -> str:
        s = self.status
        tape = "-" if s["recorded"] is None else f"{s['recorded']:,}"
        return (
            f"  {s['elapsed']:>7.0f}s  ticks {s['ticks']:>9,}  tape {tape:>9}  "
            f"pos {s['position']:>+6}  eq {s['equity']:>+11,.2f}  "
            f"ord {s['orders']:>5} rej {s['rejected']:>5} fill {s['fills']:>5}  "
            f"{s['state']}"
        )

    def run_until(
        self, api_key: str, access_token: str, seconds: float | None, ticker=None
    ) -> Result:
        """Blocking loop with periodic status and clean shutdown.

        A trip does not end the session: the governor refuses new orders and
        flatten-on-trip closes risk, while the tape records to the deadline.
        """
        if ticker is None:
            self.start(api_key, access_token)
        else:
            self.start(api_key, access_token, ticker=ticker)
        noticed = False
        try:
            while seconds is None or (time.time() - self._started_at) < seconds:
                self._sleep(self.cfg.status_every)
                print(self.status_line(), flush=True)
                if self.governor.tripped and not noticed:
                    noticed = True
                    print(
                        f"\n  GOVERNOR TRIPPED: {self.governor.stats.trip_reason} — "
                        f"{self.governor.stats.trip_detail}\n  no new orders; "
                        f"recording continues until the deadline.",
                        flush=True,
                    )
        except KeyboardInterrupt:
            print("\n  interrupted", flush=True)
        finally:
            result = self.stop()
        return result


# --------------------------------------------------------------------------
# paper vs backtest divergence — the actual product of a paper run
# --------------------------------------------------------------------------


@dataclass
class Divergence:
    """How far the simulator drifted from the tape it was simulating."""

    paper_net: float
    backtest_net: float
    paper_fills: int
    backtest_fills: int
    paper_maker_ratio: float
    backtest_maker_ratio: float

    @property
    def net_gap(self) -> float:
        return self.paper_net - self.backtest_net

    @property
    def fill_gap(self) -> int:
        return self.paper_fills - self.backtest_fills

    @property
    def optimistic(self) -> bool:
        """True when the backtest flattered the strategy relative to paper.

        This is the failure direction that costs money, and the one to assume
        is present until a long paper record says otherwise.
        """
        return self.backtest_net > self.paper_net

    def summary(self) -> str:
        verdict = (
            "BACKTEST WAS OPTIMISTIC — trust paper, not the backtest"
            if self.optimistic
            else "backtest was conservative relative to paper"
        )
        return "\n".join([
            f"{'':<18}{'paper':>14}{'backtest':>14}{'gap':>14}",
            f"{'net P&L':<18}{self.paper_net:>14,.2f}"
            f"{self.backtest_net:>14,.2f}{self.net_gap:>+14,.2f}",
            f"{'fills':<18}{self.paper_fills:>14,}"
            f"{self.backtest_fills:>14,}{self.fill_gap:>+14,}",
            f"{'maker ratio':<18}{self.paper_maker_ratio:>13.0%}"
            f"{self.backtest_maker_ratio:>14.0%}"
            f"{self.paper_maker_ratio - self.backtest_maker_ratio:>+13.0%}",
            "",
            verdict,
        ])


def compare(paper: Result, replayed: Result) -> Divergence:
    """Compare a live paper run against the same session replayed offline."""
    return Divergence(
        paper_net=paper.net_pnl,
        backtest_net=replayed.net_pnl,
        paper_fills=paper.fills,
        backtest_fills=replayed.fills,
        paper_maker_ratio=paper.maker_ratio,
        backtest_maker_ratio=replayed.maker_ratio,
    )


def save_report(
    result: Result,
    governor: RiskGovernor,
    path: Path,
    *,
    strategy_spec: str = "",
    strategy_params: dict | None = None,
    tokens: list[int] | None = None,
    watch: list[int] | None = None,
    segment: Segment | str = "option",
    engine_config: BacktestConfig | None = None,
) -> None:
    """Persist a paper session with enough setup to replay it exactly.

    Pass the trader's `engine.cfg` as `engine_config`; without it the defaults are recorded.
    """
    ecfg = engine_config or BacktestConfig(session=result.session)
    payload = {
        "strategy": result.strategy,
        "session": result.session,
        "ticks": result.ticks,
        "orders": result.orders,
        "orders_rejected": result.orders_rejected,
        "fills": result.fills,
        "maker_ratio": round(result.maker_ratio, 4),
        "round_trips": len(result.round_trips),
        "gross_pnl": round(result.gross_pnl, 2),
        "charges": round(result.charges.total, 2),
        "net_pnl": round(result.net_pnl, 2),
        "expectancy_points": round(result.expectancy_points, 5),
        "max_drawdown": round(result.max_drawdown, 2),
        "win_rate": round(result.win_rate, 4),
        "risk": governor.stats.as_dict(),
        "limits": limits_to_dict(governor.limits),
        "strategy_spec": strategy_spec,
        "strategy_params": dict(strategy_params or {}),
        "tokens": None if tokens is None else list(tokens),
        "watch": list(watch or []),
        "segment": Segment(segment).value,
        "fill_log": [list(f) for f in result.fill_log],
        "fills_config": asdict(ecfg.fills),
        "flatten_at_end": ecfg.flatten_at_end,
        "rates": asdict(ecfg.rates),
        "opening_qty": ecfg.opening_qty,
        "opening_price": ecfg.opening_price,
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------
# paper vs replay gap, rebuilt from the report
# --------------------------------------------------------------------------


@dataclass
class Gap:
    """Paper against a replay of its own tape, with its own limits and strategy."""

    session: str
    paper_net: float
    replay_net: float
    paper_fills: int
    replay_fills: int
    matched_fills: int
    first_divergence_ns: int | None
    paper_maker_ratio: float
    replay_maker_ratio: float
    ticks_live: int
    ticks_replayed: int
    ticks_dropped: int
    paper_tripped: bool
    replay_tripped: bool

    @property
    def net_gap(self) -> float:
        return self.paper_net - self.replay_net

    @property
    def match_rate(self) -> float | None:
        """None when neither side filled: nothing to match."""
        if not self.paper_fills and not self.replay_fills:
            return None
        return self.matched_fills / max(self.paper_fills, self.replay_fills)

    @property
    def optimistic(self) -> bool:
        return self.replay_net > self.paper_net

    def as_dict(self) -> dict:
        mr = self.match_rate
        return {**asdict(self), "net_gap": round(self.net_gap, 2),
                "match_rate": None if mr is None else round(mr, 6),
                "optimistic": self.optimistic}

    def summary(self) -> str:
        if self.first_divergence_ns is None:
            first = "none"
        else:
            s = self.first_divergence_ns / 1e9
            first = time.strftime("%H:%M:%S", time.localtime(s)) + f".{int(s % 1 * 1000):03d}"
        lines = [
            f"{'':<18}{'paper':>14}{'replay':>14}{'gap':>14}",
            f"{'net P&L':<18}{self.paper_net:>14,.2f}{self.replay_net:>14,.2f}"
            f"{self.net_gap:>+14,.2f}",
            f"{'fills':<18}{self.paper_fills:>14,}{self.replay_fills:>14,}"
            f"{self.paper_fills - self.replay_fills:>+14,}",
            f"{'maker ratio':<18}{self.paper_maker_ratio:>13.0%}{self.replay_maker_ratio:>14.0%}"
            f"{self.paper_maker_ratio - self.replay_maker_ratio:>+13.0%}",
            f"{'ticks':<18}{self.ticks_live:>14,}{self.ticks_replayed:>14,}"
            f"{self.ticks_live - self.ticks_replayed:>+14,}",
            f"{'tripped':<18}{str(self.paper_tripped):>14}{str(self.replay_tripped):>14}",
            "",
            f"matched fills     {self.matched_fills:,} "
            f"({'n/a' if self.match_rate is None else f'{self.match_rate:.1%}'})",
            f"first divergence  {first}",
            f"recorder drops    {self.ticks_dropped:,}",
        ]
        if self.ticks_dropped:
            lines.append("  the tape has holes; flag this session")
        return "\n".join(lines)


def match_fills(
    paper: list, replayed: list, tol_ns: int = 50_000_000
) -> tuple[int, int | None]:
    """Greedy one-to-one match in time order on token, side, qty, price and |dts| <= tol.

    Returns the matched count and the earliest unmatched ts_ns on either side.
    """
    p = sorted(paper, key=lambda f: f[0])
    r = sorted(replayed, key=lambda f: f[0])
    rts = [f[0] for f in r]
    used = [False] * len(r)
    matched = 0
    unmatched: list[int] = []
    for f in p:
        j = bisect.bisect_left(rts, f[0] - tol_ns)
        while j < len(r) and rts[j] <= f[0] + tol_ns:
            g = r[j]
            if (not used[j] and g[1] == f[1] and g[2] == f[2] and g[4] == f[4]
                    and abs(g[3] - f[3]) < 1e-9):
                used[j] = True
                matched += 1
                break
            j += 1
        else:
            unmatched.append(f[0])
    unmatched += [r[j][0] for j in range(len(r)) if not used[j]]
    return matched, (min(unmatched) if unmatched else None)


_REPLAY_KEYS = ("limits", "strategy_params", "tokens", "segment", "fill_log",
                "fills_config", "flatten_at_end", "rates")


def replay_gap(session: str, root: Path | None = None) -> Gap:
    """Replay a paper session with the strategy, limits and tokens its report recorded."""
    root = Path(root) if root else TICKS_DIR
    path = root / f"{session}.report.json"
    rep = json.loads(path.read_text())
    missing = [k for k in _REPLAY_KEYS if k not in rep]
    if missing:
        raise ValueError(f"{path} has no {', '.join(missing)}; it cannot be replayed exactly")
    if not rep["tokens"]:
        raise ValueError(f"{path} records no traded tokens; replay would trade the whole tape")
    strategy = build_strategy(rep.get("strategy_spec") or rep["strategy"], rep["strategy_params"])
    # The kill file is a live operator action; a halt shows up as paper_tripped.
    governor = RiskGovernor(replace(limits_from_dict(rep["limits"]), kill_file=None))
    engine = Engine(
        BacktestConfig(session=session, root=root, tokens=rep["tokens"],
                       segment=Segment(rep["segment"]), fills=FillConfig(**rep["fills_config"]),
                       rates=Rates(**rep["rates"]), flatten_at_end=rep["flatten_at_end"],
                       event_clock=True, opening_qty=rep.get("opening_qty", 0),
                       opening_price=rep.get("opening_price", 0.0)),
        strategy,
        governor,
    )
    # Mirrors PaperTrader.on_ticks: a strategy exception trips MANUAL, the rest
    # of that batch (same stamp) is still stepped, then stepping stops.
    halted_at = None
    for ev in replay(session, root=root, tokens=rep["tokens"]):
        if halted_at is not None and ev.ts_ns != halted_at:
            break
        try:
            engine.step(ev.tick, ev.ts_ns)
        except Exception as exc:  # noqa: BLE001
            governor.trip(Reason.MANUAL, f"strategy raised {type(exc).__name__}: {exc}")
            halted_at = ev.ts_ns
    replayed = engine.finalize()
    paper_log = rep["fill_log"]
    matched, first = match_fills(paper_log, replayed.fill_log)
    return Gap(
        session=session,
        # The report rounds to paise; compare like with like.
        paper_net=rep["net_pnl"],
        replay_net=round(replayed.net_pnl, 2),
        paper_fills=rep["fills"],
        replay_fills=replayed.fills,
        matched_fills=matched,
        first_divergence_ns=first,
        paper_maker_ratio=sum(1 for f in paper_log if f[5]) / len(paper_log) if paper_log else 0.0,
        replay_maker_ratio=replayed.maker_ratio,
        ticks_live=rep["ticks"],
        ticks_replayed=replayed.ticks,
        ticks_dropped=session_meta(session, root).get("ticks_dropped", 0),
        paper_tripped=rep["risk"]["trip_reason"] is not None,
        replay_tripped=governor.stats.trip_reason is not None,
    )


def append_gap(gap: Gap, path: Path) -> None:
    """One JSON line per compared session."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as fh:
        fh.write(json.dumps(gap.as_dict()) + "\n")

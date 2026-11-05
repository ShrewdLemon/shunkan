"""The strategy interface, and the deliberately empty slot you fill in.

A strategy is a pure reaction to ticks. It may not read files, call brokers,
or look at the clock — everything it is allowed to know arrives on `ctx`.
That restriction is what makes the same object usable in `engine.backtest`
and against a live socket without a second implementation to keep in sync.

Write strategies that are boring to test: no randomness, no global state, and
no dependence on anything that will not exist at 09:15 tomorrow.
"""

from __future__ import annotations

import importlib
import inspect
import json
from typing import Protocol, runtime_checkable

from shunkan.algo.fills import Fill


@runtime_checkable
class Strategy(Protocol):
    """Implement `on_tick`; everything else is optional."""

    def on_tick(self, ctx) -> None:
        """Called once per tick, in order. Place/cancel orders via `ctx`."""
        ...


class BaseStrategy:
    """Convenience base with no-op hooks, so subclasses override only what
    they use. Subclassing is optional — any object with `on_tick` works."""

    name: str = "base"

    def on_start(self, ctx) -> None:  # noqa: D401
        """Called once before the first tick."""

    def on_tick(self, ctx) -> None:
        raise NotImplementedError

    def on_fill(self, ctx, fill: Fill) -> None:
        """Called for each fill, after the position has been updated."""

    def on_finish(self, ctx) -> None:
        """Called once after the last tick, before results are computed."""


class NullStrategy(BaseStrategy):
    """Trades nothing. The harness's control: run it over any session and the
    result must be exactly zero P&L and zero charges. If it is not, the bug
    is in the engine, not in your idea."""

    name = "null"

    def on_tick(self, ctx) -> None:
        return


class QuoteBothSides(BaseStrategy):
    """Reference implementation, NOT a suggestion to trade it.

    Posts a passive bid and offer `edge` points either side of the microprice
    and re-quotes when the market moves away. It exists to exercise the maker
    path end-to-end — queueing, partial fills, inventory, charges — so you can
    see the harness work before you have an idea of your own.

    Run it and you will most likely watch it lose money to adverse selection:
    it gets filled precisely when the market is about to move against it. That
    is the correct lesson and it is cheaper to learn here.
    """

    name = "quote-both-sides"

    def __init__(self, edge: float = 1.0, size: int = 65, max_position: int = 195):
        self.edge = edge
        self.size = size
        self.max_position = max_position

    def on_tick(self, ctx) -> None:
        tick = ctx.tick
        if not tick.bids or not tick.asks:
            return
        fair = tick.microprice
        want_bid = round(fair - self.edge, 2)
        want_ask = round(fair + self.edge, 2)

        # Re-quote only when our resting price is stale, otherwise we lose
        # queue position every tick and never fill anything.
        for o in ctx.working_orders:
            target = want_bid if o.side.value == "BUY" else want_ask
            if o.limit_price is None or abs(o.limit_price - target) > self.edge / 2:
                ctx.cancel(o.id)

        live_sides = {o.side.value for o in ctx.working_orders if not o.done}
        if "BUY" not in live_sides and ctx.position < self.max_position:
            ctx.buy(self.size, limit=want_bid)
        if "SELL" not in live_sides and ctx.position > -self.max_position:
            ctx.sell(self.size, limit=want_ask)


BUILTIN = {"null": NullStrategy, "quote-both-sides": QuoteBothSides}


def build_strategy(spec: str, params: dict | None = None):
    """`quote-both-sides`, `null`, or `package.module:ClassName`; params as kwargs."""
    kw = params or {}
    if spec in BUILTIN:
        return BUILTIN[spec](**kw)
    if ":" not in spec:
        raise SystemExit(
            f"unknown strategy {spec!r}; use one of {sorted(BUILTIN)} "
            f"or 'module:ClassName'"
        )
    mod_name, cls_name = spec.split(":", 1)
    cls = getattr(importlib.import_module(mod_name), cls_name, None)
    if cls is None:
        raise ImportError(f"{mod_name} has no {cls_name}")
    return cls(**kw)


def _json_safe(v) -> bool:
    try:
        return json.loads(json.dumps(v, allow_nan=False)) == v
    except (TypeError, ValueError):
        return False


def strategy_params(strategy) -> dict:
    """Constructor arguments recovered from the instance, so a report can rebuild it.

    A parameter is read from the attribute of the same name, or `_name`. State
    the constructor does not take is left out. With `**kwargs`, every public
    scalar attribute is passed too. Raises ValueError when a required argument
    cannot be recovered, a recovered one does not survive JSON, or the class
    does not construct from the result.
    """
    cls = type(strategy)
    out = {} if cls.__init__ is object.__init__ else _ctor_params(strategy)
    try:
        cls(**out)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{cls.__name__}(**{out}) does not rebuild: {exc}") from exc
    return out


def _ctor_params(strategy) -> dict:
    cls = type(strategy)
    attrs = vars(strategy)
    out: dict = {}
    var_kw = False
    for p in list(inspect.signature(cls.__init__).parameters.values())[1:]:
        if p.kind is p.VAR_KEYWORD:
            var_kw = True
            continue
        if p.kind is p.VAR_POSITIONAL:
            continue
        key = next((k for k in (p.name, f"_{p.name}") if k in attrs), None)
        if key is None or p.kind is p.POSITIONAL_ONLY:
            if p.default is p.empty:
                raise ValueError(
                    f"{cls.__name__}: cannot recover argument {p.name!r} from the instance"
                )
            continue
        if not _json_safe(attrs[key]):
            raise ValueError(f"{cls.__name__}: argument {p.name!r} is not JSON: {attrs[key]!r}")
        out[p.name] = attrs[key]
    if var_kw:
        for k, v in attrs.items():
            if not k.startswith("_") and k not in out and isinstance(v, (int, float, str, bool)):
                out[k] = v
    return out

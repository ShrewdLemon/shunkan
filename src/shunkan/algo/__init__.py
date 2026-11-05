"""Tick-level research harness: record depth, replay it, execute against it.

    from shunkan.algo import TickRecorder, BacktestConfig, backtest

Deliberately separate from `shunkan.backtest`, which works on daily/intraday
bars. Bars cannot answer execution questions: a scalping result is decided by
the spread you crossed and the queue you sat in, and neither survives being
aggregated into an OHLC candle.
"""

from shunkan.algo.book import Sweep, imbalance, is_tradeable, slippage, sweep_buy, sweep_sell
from shunkan.algo.charges import (
    DEFAULT_RATES,
    ChargeBreakdown,
    Rates,
    Segment,
    breakeven_points,
    charge_leg,
    charge_round_trip,
    exercise_stt,
)
from shunkan.algo.engine import BacktestConfig, Context, Engine, Position, backtest
from shunkan.algo.fills import Fill, FillConfig, FillEngine, Order, OrderStatus, Side
from shunkan.algo.metrics import Result, RoundTrip
from shunkan.algo.paper import (
    Divergence,
    Gap,
    PaperConfig,
    PaperTrader,
    append_gap,
    compare,
    match_fills,
    replay_gap,
    save_report,
)
from shunkan.algo.risk import (
    ALLOW,
    Decision,
    Reason,
    RiskGovernor,
    RiskLimits,
    RiskState,
    RiskStats,
    limits_from_dict,
    limits_to_dict,
)
from shunkan.algo.recorder import TickRecorder, RecorderStats, record_live
from shunkan.algo.replay import Event, replay, replay_frame, session_meta, session_parts
from shunkan.algo.strategy import (
    BaseStrategy,
    NullStrategy,
    QuoteBothSides,
    Strategy,
    build_strategy,
    strategy_params,
)

__all__ = [
    "Sweep", "imbalance", "is_tradeable", "slippage", "sweep_buy", "sweep_sell",
    "DEFAULT_RATES", "ChargeBreakdown", "Rates", "Segment",
    "breakeven_points", "charge_leg", "charge_round_trip", "exercise_stt",
    "BacktestConfig", "Context", "Engine", "Position", "backtest",
    "Fill", "FillConfig", "FillEngine", "Order", "OrderStatus", "Side",
    "Result", "RoundTrip",
    "Divergence", "PaperConfig", "PaperTrader", "compare", "save_report",
    "Gap", "append_gap", "match_fills", "replay_gap",
    "ALLOW", "Decision", "Reason", "RiskGovernor", "RiskLimits",
    "RiskState", "RiskStats", "limits_from_dict", "limits_to_dict",
    "TickRecorder", "RecorderStats", "record_live",
    "Event", "replay", "replay_frame", "session_meta", "session_parts",
    "BaseStrategy", "NullStrategy", "QuoteBothSides", "Strategy",
    "build_strategy", "strategy_params",
]

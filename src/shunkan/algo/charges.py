"""Exact Indian F&O transaction costs.

Every number a backtest reports is a lie until this module is subtracted from
it. For a scalper it is not a rounding error — it is most of the decision.
Measured on a real NIFTY option round trip (30 lots, 69.20 -> 72.00) these
charges came to Rs 305.79 against Rs 5,460 gross, or 0.157 option points per
round trip. The bid-ask spread on the same underlying was 1.50 points, so
crossing costs roughly nine times what the taxman does; see `fills.py`.

Rates are statutory and change by circular. They are dated and centralised in
`Rates` rather than sprinkled through the engine so a change is one edit;
`rates_on(day)` returns the set in force that day. STT on F&O rose from
1 April 2026 (NSE's STT table: option sale 0.10% -> 0.15% of premium, exercise
0.125% -> 0.15% of intrinsic, futures 0.02% -> 0.05%). From 1 March 2026 NSE
moved its IPFT levy into the transaction charge with the total unchanged
(NSE/FA/73061). Rates before October 2024 are not modelled. Verify against
your own contract notes before sizing up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Segment(str, Enum):
    OPTION = "option"
    FUTURE = "future"


@dataclass(frozen=True)
class Rates:
    """Statutory + broker rates. Defaults are Zerodha / NSE from 1 April 2026."""

    # Broker. Zerodha charges a flat Rs 20 per executed order on F&O, or
    # 0.03% of turnover, whichever is lower. For options the flat fee always
    # binds at any realistic premium, but futures turnover is notional and
    # large, so the percentage leg matters there.
    brokerage_flat: float = 20.0
    brokerage_pct: float = 0.0003
    brokerage_pct_applies: bool = True

    # Securities Transaction Tax — sell side only.
    stt_option_sell: float = 0.0015      # 0.15% of premium
    stt_future_sell: float = 0.0005      # 0.05% of notional
    # Exercised/assigned ITM options are taxed on INTRINSIC value, not premium.
    # Note the direction of this trade-off is the opposite of the folklore:
    # see `exercise_stt` for the arithmetic.
    stt_option_exercise: float = 0.0015  # 0.15% of intrinsic

    # Exchange transaction charges (NSE).
    exch_option: float = 0.000355299     # Rs 3,552.99 per crore of premium
    exch_future: float = 0.000018299     # Rs 182.99 per crore of notional

    # Investor Protection Fund Trust levy (NSE).
    ipft_option: float = 0.000000001     # Rs 0.01 per crore
    ipft_future: float = 0.000000001     # Rs 0.01 per crore

    sebi: float = 0.000001               # Rs 10 per crore, both sides
    stamp_option_buy: float = 0.00003    # 0.003% of premium, buy side
    stamp_future_buy: float = 0.00002    # 0.002% of notional, buy side
    gst: float = 0.18                    # on brokerage + exchange + SEBI + IPFT


RATES_UNTIL_MARCH_2026 = Rates(
    stt_option_sell=0.0010, stt_future_sell=0.0002, stt_option_exercise=0.00125,
    exch_option=0.0003503, exch_future=0.0000173,
    ipft_option=0.000005, ipft_future=0.000001,
)
DEFAULT_RATES = Rates()
NEW_RATES_FROM = date(2026, 4, 1)


def rates_on(day: date) -> Rates:
    """Rates in force on `day`."""
    return DEFAULT_RATES if day >= NEW_RATES_FROM else RATES_UNTIL_MARCH_2026


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    ipft: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.brokerage + self.stt + self.exchange
            + self.ipft + self.sebi + self.stamp + self.gst
        )

    def __add__(self, other: "ChargeBreakdown") -> "ChargeBreakdown":
        return ChargeBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange=self.exchange + other.exchange,
            ipft=self.ipft + other.ipft,
            sebi=self.sebi + other.sebi,
            stamp=self.stamp + other.stamp,
            gst=self.gst + other.gst,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage, "stt": self.stt,
            "exchange": self.exchange, "ipft": self.ipft, "sebi": self.sebi,
            "stamp": self.stamp, "gst": self.gst, "total": self.total,
        }


def _brokerage(turnover: float, r: Rates) -> float:
    if not r.brokerage_pct_applies:
        return r.brokerage_flat
    return min(r.brokerage_flat, turnover * r.brokerage_pct)


def charge_leg(
    price: float,
    quantity: int,
    is_buy: bool,
    segment: Segment = Segment.OPTION,
    rates: Rates = DEFAULT_RATES,
) -> ChargeBreakdown:
    """Charges for ONE executed order. Turnover is premium for options.

    Called per fill, not per round trip, because a partially-filled order that
    completes in three prints is billed as one executed order by the broker but
    three times by the exchange. Modelling it per leg keeps the two separable.
    """
    if quantity <= 0 or price < 0:
        return ChargeBreakdown()
    turnover = price * quantity
    opt = segment is Segment.OPTION

    brokerage = _brokerage(turnover, rates)
    stt = 0.0 if is_buy else turnover * (
        rates.stt_option_sell if opt else rates.stt_future_sell
    )
    exchange = turnover * (rates.exch_option if opt else rates.exch_future)
    ipft = turnover * (rates.ipft_option if opt else rates.ipft_future)
    sebi = turnover * rates.sebi
    stamp = (
        turnover * (rates.stamp_option_buy if opt else rates.stamp_future_buy)
        if is_buy else 0.0
    )
    gst = rates.gst * (brokerage + exchange + sebi + ipft)
    return ChargeBreakdown(
        brokerage=brokerage, stt=stt, exchange=exchange,
        ipft=ipft, sebi=sebi, stamp=stamp, gst=gst,
    )


def charge_round_trip(
    buy_price: float,
    sell_price: float,
    quantity: int,
    segment: Segment = Segment.OPTION,
    rates: Rates = DEFAULT_RATES,
) -> ChargeBreakdown:
    """Both legs of a completed trade."""
    return (
        charge_leg(buy_price, quantity, True, segment, rates)
        + charge_leg(sell_price, quantity, False, segment, rates)
    )


def exercise_stt(intrinsic: float, quantity: int, rates: Rates = DEFAULT_RATES) -> float:
    """STT on an ITM option left to expire, charged on intrinsic value.

    The common advice is "always square off, never let it expire ITM". For
    CASH-SETTLED INDEX options on a flat-fee broker that is arithmetically
    wrong, and this function exists partly to show why. Selling costs
    0.15% STT + 0.03553% exchange + GST ~= 0.192% of turnover plus the flat
    fee; exercise costs 0.15% of intrinsic and no fee. Expiring
    is cheaper at every intrinsic value we tested (see tests). The gap is
    small either way -- a few hundred rupees on 30 lots.

    Where the folklore IS right, and badly so, is STOCK options: NSE settles
    those PHYSICALLY. An ITM stock option carried into expiry obliges you to
    deliver or take delivery of the full notional, not the intrinsic. That is
    a margin and settlement event orders of magnitude larger than any STT, and
    it is the reason to square off. This engine models index options; if you
    ever point it at stock options, model physical settlement first.
    """
    if intrinsic <= 0 or quantity <= 0:
        return 0.0
    return intrinsic * quantity * rates.stt_option_exercise


def breakeven_points(
    price: float,
    quantity: int,
    spread: float = 0.0,
    segment: Segment = Segment.OPTION,
    rates: Rates = DEFAULT_RATES,
) -> float:
    """Points the price must move for a round trip to break even.

    Pass `spread` to include the cost of crossing it on both legs (taker);
    leave it 0.0 for a pure maker who is never the aggressor. The gap between
    those two answers is the whole argument for posting rather than crossing.
    """
    charges = charge_round_trip(price, price, quantity, segment, rates).total
    return charges / quantity + spread

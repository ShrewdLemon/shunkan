"""Option chain model and dealer-positioning analytics.

Everything operates on parallel numpy arrays (one row per strike), so the
full analytics pass — PCR, max pain, OI walls, buildup classification,
expected move — is a handful of vector ops, fast enough to rerun on every
data refresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np

from shunkan.derivatives.greeks import bs_greeks, implied_vol


@dataclass
class OptionChain:
    symbol: str
    spot: float
    expiry: date
    t_years: float  # time to expiry in years
    strikes: np.ndarray  # (n,)
    call_ltp: np.ndarray
    call_oi: np.ndarray
    call_oi_change: np.ndarray
    call_volume: np.ndarray
    call_iv: np.ndarray  # NaN where unknown (computed lazily)
    put_ltp: np.ndarray
    put_oi: np.ndarray
    put_oi_change: np.ndarray
    put_volume: np.ndarray
    put_iv: np.ndarray
    source: str = "synthetic"
    # True when OI/prices are simulated rather than observed. Defaults to
    # True on purpose: a construction path that forgets to declare itself
    # real must be treated as a model, never stored, never counted.
    is_model: bool = True
    # None when no source could name the contract lot. A wrong lot silently
    # multiplies every rupee figure downstream, so there is no default and no
    # table to go stale — consumers show a dash and price per unit.
    lot_size: int | None = None
    lot_size_source: str = ""  # where the lot came from, or why we have none
    # why we ended up on this source — each skipped step with its reason
    source_trail: list[str] = field(default_factory=list)
    # every expiry the source listed for this underlying (empty when unknown)
    expiries: list[date] = field(default_factory=list)

    def ensure_iv(self, r: float = 0.065) -> None:
        """Fill missing IVs from prices (vectorized solve, both sides at once)."""
        n = len(self.strikes)
        need_c = np.isnan(self.call_iv) & (self.call_ltp > 0)
        need_p = np.isnan(self.put_iv) & (self.put_ltp > 0)
        if not (need_c.any() or need_p.any()):
            return
        prices = np.concatenate([self.call_ltp, self.put_ltp])
        strikes2 = np.concatenate([self.strikes, self.strikes])
        is_call = np.concatenate([np.ones(n, bool), np.zeros(n, bool)])
        iv = implied_vol(prices, self.spot, strikes2, self.t_years, is_call, r=r)
        self.call_iv = np.where(need_c, iv[:n], self.call_iv)
        self.put_iv = np.where(need_p, iv[n:], self.put_iv)

    def greeks(self, r: float = 0.065) -> dict[str, dict[str, np.ndarray]]:
        self.ensure_iv(r)
        call_g = bs_greeks(self.spot, self.strikes, self.t_years,
                           np.nan_to_num(self.call_iv, nan=0.15), True, r)
        put_g = bs_greeks(self.spot, self.strikes, self.t_years,
                          np.nan_to_num(self.put_iv, nan=0.15), False, r)
        return {"call": call_g, "put": put_g}

    @property
    def atm_index(self) -> int:
        return int(np.argmin(np.abs(self.strikes - self.spot)))


BUILDUP_LABELS = {
    (1, 1): "long buildup",       # price up, OI up — new longs
    (-1, 1): "short buildup",     # price down, OI up — new shorts
    (-1, -1): "long unwinding",   # price down, OI down — longs exiting
    (1, -1): "short covering",    # price up, OI down — shorts exiting
}


def classify_buildup(price_change: float, oi_change: float) -> str:
    if price_change == 0 or oi_change == 0:
        return "neutral"
    return BUILDUP_LABELS[(int(np.sign(price_change)), int(np.sign(oi_change)))]


@dataclass
class ChainAnalytics:
    pcr_oi: float
    pcr_volume: float
    max_pain: float
    support: float          # strike with the largest put OI wall
    resistance: float       # strike with the largest call OI wall
    atm_strike: float
    atm_iv: float           # mean of ATM call/put IV
    straddle_price: float   # ATM call + put
    expected_move_pct: float  # straddle / spot — market-implied move by expiry
    call_oi_total: float
    put_oi_total: float
    unusual: list[dict] = field(default_factory=list)  # vol >> OI strikes
    bias: str = "neutral"
    bias_reason: str = ""


def analyze_chain(chain: OptionChain, unusual_ratio: float = 3.0) -> ChainAnalytics:
    chain.ensure_iv()
    s = chain.strikes
    c_oi, p_oi = chain.call_oi.astype(float), chain.put_oi.astype(float)
    c_vol, p_vol = chain.call_volume.astype(float), chain.put_volume.astype(float)

    call_total, put_total = float(c_oi.sum()), float(p_oi.sum())
    pcr_oi = put_total / call_total if call_total > 0 else 0.0
    vol_c, vol_p = float(c_vol.sum()), float(p_vol.sum())
    pcr_vol = vol_p / vol_c if vol_c > 0 else 0.0

    # Max pain: expiry level minimizing total option-buyer payoff.
    # Payoff matrix via broadcasting: rows = candidate expiry at each strike.
    expiry_px = s[:, None]
    call_pay = np.maximum(expiry_px - s[None, :], 0.0) @ c_oi
    put_pay = np.maximum(s[None, :] - expiry_px, 0.0) @ p_oi
    max_pain = float(s[np.argmin(call_pay + put_pay)])

    support = float(s[np.argmax(p_oi)]) if put_total > 0 else float(s[0])
    resistance = float(s[np.argmax(c_oi)]) if call_total > 0 else float(s[-1])

    i = chain.atm_index
    atm_strike = float(s[i])
    ivs = [v for v in (chain.call_iv[i], chain.put_iv[i]) if not np.isnan(v)]
    atm_iv = float(np.mean(ivs)) if ivs else float("nan")
    straddle = float(chain.call_ltp[i] + chain.put_ltp[i])
    expected_move = straddle / chain.spot if chain.spot > 0 else 0.0

    unusual: list[dict] = []
    for side, vol, oi, ltp in (("CALL", c_vol, c_oi, chain.call_ltp),
                               ("PUT", p_vol, p_oi, chain.put_ltp)):
        mask = (vol > unusual_ratio * np.maximum(oi, 1.0)) & (vol > 0) & (oi >= 0)
        for j in np.flatnonzero(mask):
            unusual.append({
                "side": side, "strike": float(s[j]), "volume": float(vol[j]),
                "oi": float(oi[j]), "ratio": float(vol[j] / max(oi[j], 1.0)),
                "ltp": float(ltp[j]),
            })
    unusual.sort(key=lambda u: -u["ratio"])

    bias, reason = _bias(pcr_oi, chain.spot, max_pain, support, resistance)

    return ChainAnalytics(
        pcr_oi=pcr_oi,
        pcr_volume=pcr_vol,
        max_pain=max_pain,
        support=support,
        resistance=resistance,
        atm_strike=atm_strike,
        atm_iv=atm_iv,
        straddle_price=straddle,
        expected_move_pct=expected_move,
        call_oi_total=call_total,
        put_oi_total=put_total,
        unusual=unusual[:10],
        bias=bias,
        bias_reason=reason,
    )


def _bias(pcr: float, spot: float, max_pain: float, support: float, resistance: float):
    """Simple, transparent positioning read — not a trade signal."""
    notes = []
    score = 0
    if pcr > 1.2:
        score += 1
        notes.append(f"PCR {pcr:.2f} — heavy put writing (bullish positioning)")
    elif pcr < 0.8:
        score -= 1
        notes.append(f"PCR {pcr:.2f} — heavy call writing (bearish positioning)")
    else:
        notes.append(f"PCR {pcr:.2f} — balanced")
    drift = (max_pain - spot) / spot if spot else 0.0
    if abs(drift) > 0.005:
        score += 1 if drift > 0 else -1
        notes.append(f"max pain {max_pain:g} is {drift:+.1%} from spot (expiry magnet)")
    if spot and support and resistance:
        room_up = (resistance - spot) / spot
        room_dn = (spot - support) / spot
        notes.append(f"OI walls: support {support:g} / resistance {resistance:g} "
                     f"({room_dn:+.1%} / {room_up:+.1%} away)")
    label = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return label, "; ".join(notes)

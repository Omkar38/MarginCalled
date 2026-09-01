"""Convert a detected rectangle into an order Alpaca will accept.

The paper's retained denominations are price-weighted ratio positions. For the T1
denomination the weights are B_w = price(B) on the long leg A and C_w = price(C) on
the short leg D, chosen so the premium received at entry equals the violation size:

    C_w * D^bid - B_w * A^ask  =  C^bid D^bid - A^ask B^ask  =  V.

That construction cannot be submitted as-is. Since K1~ < K2 at the shared maturity
T2, contract C sits at the lower strike and is always the dearer of the two, so
C_w > B_w on every rectangle: the short leg always outnumbers the long leg, and a
multi-leg order with uncovered short calls is rejected.

Two executable forms are offered, and neither preserves the credit-equals-violation
property. That is stated rather than hidden:

  TWO_LEG   Cap shorts at longs -> long A, short D at 1:1, same expiry T1.
            Since K1 < K2~, this is a long-lower/short-higher call spread: a
            DEBIT bull call spread. Defined risk, but directionally bullish, and
            economically a different trade from the one the paper describes.

  FOUR_LEG  long A, long B, short C, short D at 1:1:1:1. This decomposes into two
            verticals, one per expiry:
                T1: long K1, short K2~   (K1 < K2~)  -> debit call spread
                T2: long K2, short K1~   (K1~ < K2)  -> credit call spread
            Both are covered, both are defined-risk, and the position uses all
            four contracts of the detected rectangle. Four legs is Alpaca's limit.

FOUR_LEG is the default: it is fully covered, it keeps the rectangle intact, and
its worst case is bounded at both expiries.

Max loss is reported as the sum of the two verticals' worst cases. Because the
legs settle at different dates, that sum is an upper bound rather than a realisable
simultaneous loss - which is the direction a risk limit should err in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import gcd

from .rectangles import RectangleCandidate

__all__ = [
    "Structure",
    "structure_for",
    "config_for",
    "EUROPEAN_UNDERLYINGS",
    "Side",
    "PositionLeg",
    "PositionSpec",
    "PositionConfig",
    "theoretical_weights",
    "integer_ratio",
    "build_position",
]

CONTRACT_MULTIPLIER = 100.0

# Transaction cost per contract, per side. Three conventions, kept explicit
# because the choice materially changes the economics.
#
#   ALPACA_PAPER        What the competition actually scores. Alpaca is
#                       commission-free on US-listed options through the API,
#                       and paper trading simulates no fees at all.
#   ALPACA_REGULATORY   What live Alpaca would cost: ORF $0.02295 + OCC $0.025
#                       + TAF $0.00329 on sells. Not charged in paper, but this
#                       is the honest figure for any live-deployability claim.
#   IBKR_LITE           $0.65/side, i.e. $1.30 per contract per leg round trip.
#                       The convention used in the source study, retained so its
#                       results can be reproduced for comparison.
COMMISSION_ALPACA_PAPER = 0.0
COMMISSION_ALPACA_REGULATORY = 0.0483
COMMISSION_IBKR_LITE = 0.65

# Default to what the contest scores. Anything claiming live economics should
# pass COMMISSION_ALPACA_REGULATORY explicitly and say so.
COMMISSION_PER_CONTRACT_SIDE = COMMISSION_ALPACA_PAPER


class Structure(str, Enum):
    TWO_LEG = "two_leg"
    FOUR_LEG = "four_leg"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class PositionLeg:
    symbol: str
    strike: float
    expiry: str
    side: Side
    ratio_qty: int
    entry_price: float  # ask if buying, bid if selling

    def to_alpaca_leg(self) -> dict:
        """Leg payload shape for an Alpaca MLeg order.

        Returned as data only. This module never submits anything.
        """
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "ratio_qty": str(self.ratio_qty),
            "position_intent": (
                "buy_to_open" if self.side is Side.BUY else "sell_to_open"
            ),
        }


@dataclass(frozen=True)
class PositionConfig:
    structure: Structure = Structure.FOUR_LEG
    max_ratio_denominator: int = 1  # 1 => force 1:1, the only always-covered form
    commission_per_contract_side: float = COMMISSION_PER_CONTRACT_SIDE
    require_covered: bool = True


@dataclass
class PositionSpec:
    """An executable position, with its economics recomputed after rounding."""

    candidate: RectangleCandidate
    structure: Structure
    legs: list[PositionLeg] = field(default_factory=list)

    entry_cash: float = 0.0  # per 1-lot, dollars. Positive = credit received.
    max_loss: float = 0.0  # per 1-lot, dollars, before commissions
    commissions_round_trip: float = 0.0
    net_delta: float | None = None

    theoretical_long_weight: float = 0.0
    theoretical_short_weight: float = 0.0
    is_covered: bool = False
    rejected_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_executable(self) -> bool:
        return self.rejected_reason is None and bool(self.legs)

    @property
    def max_loss_with_commissions(self) -> float:
        return self.max_loss + self.commissions_round_trip

    @property
    def coverage_ratio(self) -> float:
        """C_w / B_w, the factor by which the theoretical position is short-heavy."""
        if self.theoretical_long_weight <= 0:
            return float("inf")
        return self.theoretical_short_weight / self.theoretical_long_weight

    def to_record(self) -> dict:
        return {
            "structure": self.structure.value,
            "episode_key": self.candidate.episode_key,
            "legs": [
                {
                    "symbol": leg.symbol,
                    "side": leg.side.value,
                    "ratio_qty": leg.ratio_qty,
                    "strike": leg.strike,
                    "expiry": leg.expiry,
                    "entry_price": leg.entry_price,
                }
                for leg in self.legs
            ],
            "entry_cash": self.entry_cash,
            "max_loss": self.max_loss,
            "max_loss_with_commissions": self.max_loss_with_commissions,
            "commissions_round_trip": self.commissions_round_trip,
            "net_delta": self.net_delta,
            "theoretical_long_weight": self.theoretical_long_weight,
            "theoretical_short_weight": self.theoretical_short_weight,
            "coverage_ratio": self.coverage_ratio,
            "is_covered": self.is_covered,
            "is_executable": self.is_executable,
            "rejected_reason": self.rejected_reason,
            "notes": list(self.notes),
        }


# Index options are European-style, and Alpaca refuses a multi-leg order whose
# European legs span different expirations:
#
#   HTTP 422, code 42210000: "European-style option legs in a multi-leg order
#   must have the same expiration date"
#
# Verified live: on SPX the four-leg rectangle and the K2 diagonal are both
# rejected, while the T1 pair - buy A, sell D, both at T1 - is accepted. So the
# structure is not a preference, it is dictated by the underlying's exercise
# style. T1 is also the denomination the source study singles out as performing
# "extraordinarily well", so the constraint costs nothing.
EUROPEAN_UNDERLYINGS = frozenset(
    {"SPX", "SPXW", "XSP", "VIX", "VIXW", "DJX", "NDX", "RUT"}
)


def structure_for(underlying: str) -> Structure:
    """The only multi-leg structure this underlying will accept.

    European -> TWO_LEG (the T1 denomination; both legs share T1).
    American  -> FOUR_LEG (spans both expiries; accepted, verified on SPY).
    """
    if underlying.upper() in EUROPEAN_UNDERLYINGS:
        return Structure.TWO_LEG
    return Structure.FOUR_LEG


def config_for(underlying: str, **kwargs) -> "PositionConfig":
    """PositionConfig with the structure the underlying permits."""
    return PositionConfig(structure=structure_for(underlying), **kwargs)


def theoretical_weights(cand: RectangleCandidate) -> tuple[float, float]:
    """(B_w, C_w) - the paper's price weights for the T1 denomination.

    B_w sits on the long leg A, C_w on the short leg D. C_w > B_w always, because
    C is the lower strike at the shared maturity T2 and therefore the dearer leg.
    """
    return cand.B.quote.mid, cand.C.quote.mid


def integer_ratio(long_w: float, short_w: float, max_denominator: int) -> tuple[int, int]:
    """Smallest integer (long, short) near the theoretical weights.

    With max_denominator == 1 this always returns (1, 1): the only ratio that is
    guaranteed covered for a call spread regardless of the weights.
    """
    if max_denominator <= 1 or long_w <= 0 or short_w <= 0:
        return 1, 1

    target = short_w / long_w
    best = (1, 1)
    best_err = abs(target - 1.0)
    for denom in range(1, max_denominator + 1):
        numer = max(1, round(target * denom))
        err = abs(target - numer / denom)
        if err < best_err:
            best_err, best = err, (denom, numer)
    g = gcd(*best)
    return best[0] // g, best[1] // g


def build_position(
    cand: RectangleCandidate, cfg: PositionConfig | None = None
) -> PositionSpec:
    """Turn a candidate into an executable, defined-risk position.

    All economics - entry cash, max loss, delta - are computed from the integer
    quantities actually being sent, never inherited from the theoretical weights.
    """
    cfg = cfg or PositionConfig()
    long_w, short_w = theoretical_weights(cand)

    spec = PositionSpec(
        candidate=cand,
        structure=cfg.structure,
        theoretical_long_weight=long_w,
        theoretical_short_weight=short_w,
    )

    if short_w <= long_w:
        spec.notes.append(
            f"Unexpected: C_w={short_w:.4f} <= B_w={long_w:.4f}. The algebra makes "
            f"C the dearer leg on every rectangle; investigate the quotes."
        )

    n_long, n_short = integer_ratio(long_w, short_w, cfg.max_ratio_denominator)
    if cfg.require_covered and n_short > n_long:
        spec.rejected_reason = (
            f"uncovered: {n_long}:{n_short} long:short would leave "
            f"{n_short - n_long} naked short call(s)"
        )
        return spec

    spec.notes.append(
        f"Theoretical weights {long_w:.4f}:{short_w:.4f} (ratio "
        f"{short_w / long_w:.3f}) capped to {n_long}:{n_short}. The paper's "
        f"credit-equals-violation property does not survive this cap."
    )

    A, B, C, D = cand.A, cand.B, cand.C, cand.D

    if cfg.structure is Structure.TWO_LEG:
        # T1 vertical only: long A (K1), short D (K2~), K1 < K2~ -> debit spread.
        debit = A.quote.ask - D.quote.bid
        spec.legs = [
            PositionLeg(A.symbol, A.strike, A.expiry.isoformat(), Side.BUY, 1, A.quote.ask),
            PositionLeg(D.symbol, D.strike, D.expiry.isoformat(), Side.SELL, 1, D.quote.bid),
        ]
        spec.entry_cash = -debit * CONTRACT_MULTIPLIER
        spec.max_loss = max(debit, 0.0) * CONTRACT_MULTIPLIER
        spec.is_covered = D.strike > A.strike
        spec.notes.append(
            "TWO_LEG is a debit bull call spread: directionally long, not a TP2 "
            "arbitrage. Included for comparison only."
        )
        n_contracts = 2
    else:
        # T1 vertical: long A (K1), short D (K2~), K1 < K2~ -> debit
        debit_t1 = A.quote.ask - D.quote.bid
        # T2 vertical: long B (K2), short C (K1~), K1~ < K2 -> credit
        credit_t2 = C.quote.bid - B.quote.ask

        width_t1 = D.strike - A.strike
        width_t2 = B.strike - C.strike

        if width_t1 <= 0 or width_t2 <= 0:
            spec.rejected_reason = (
                f"degenerate widths: T1 {width_t1:.2f}, T2 {width_t2:.2f}; "
                f"expected K1 < K2~ and K1~ < K2"
            )
            return spec

        max_loss_t1 = max(debit_t1, 0.0)
        max_loss_t2 = max(width_t2 - credit_t2, 0.0)

        spec.legs = [
            PositionLeg(A.symbol, A.strike, A.expiry.isoformat(), Side.BUY, 1, A.quote.ask),
            PositionLeg(B.symbol, B.strike, B.expiry.isoformat(), Side.BUY, 1, B.quote.ask),
            PositionLeg(C.symbol, C.strike, C.expiry.isoformat(), Side.SELL, 1, C.quote.bid),
            PositionLeg(D.symbol, D.strike, D.expiry.isoformat(), Side.SELL, 1, D.quote.bid),
        ]
        spec.entry_cash = (
            (C.quote.bid + D.quote.bid) - (A.quote.ask + B.quote.ask)
        ) * CONTRACT_MULTIPLIER
        spec.max_loss = (max_loss_t1 + max_loss_t2) * CONTRACT_MULTIPLIER
        spec.is_covered = D.strike > A.strike and C.strike < B.strike
        spec.notes.append(
            f"FOUR_LEG = two covered verticals. T1 debit {debit_t1:.2f} "
            f"(width {width_t1:.2f}); T2 credit {credit_t2:.2f} (width {width_t2:.2f}). "
            f"Max loss is the sum of both worst cases, an upper bound since the "
            f"legs settle on different dates."
        )
        n_contracts = 4

    spec.commissions_round_trip = (
        n_contracts * 2 * cfg.commission_per_contract_side * max(n_long, n_short)
    )

    if cfg.require_covered and not spec.is_covered:
        spec.rejected_reason = (
            "strike ordering does not cover the short legs; expected K1 < K2~ "
            "and K1~ < K2"
        )
        return spec

    if spec.max_loss <= 0:
        spec.rejected_reason = f"non-positive max loss ({spec.max_loss:.2f}); refusing"
        return spec

    return spec

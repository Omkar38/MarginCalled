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
from typing import NamedTuple
from math import gcd

from .rectangles import RectangleCandidate

__all__ = [
    "Structure",
    "structure_for",
    "allowed_denominations",
    "DenominationSelector",
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
    """The tradeable denominations, per Table 5.1 of the source study.

    T1  buy A (K1,T1), sell D (K2~,T1)   - both legs at T1, a vertical
    K2  buy B (K2,T2), sell D (K2~,T1)   - legs at T2 and T1, a diagonal

    Both short D, the near-dated leg, which is why the study retains exactly
    these two. FOUR_LEG trades the whole rectangle and is not one of the
    paper's denominations; it is kept for comparison only.
    """

    T1 = "two_leg"        # value kept for backward compatibility with stored data
    K2 = "k2_diagonal"
    FOUR_LEG = "four_leg"

    # Legacy alias: TWO_LEG was the original name for the T1 denomination.
    TWO_LEG = "two_leg"


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

        Quantity is asserted to be a whole positive number here, at the last
        point before the payload leaves. The source study's weights are
        continuous and it says so - "position weights can imply fractional
        contract quantities" - but options trade in whole contracts, so a
        fractional weight is a theoretical size, never an order. Enforcing it at
        the boundary means no future change to the weighting can quietly ship a
        fraction to the broker.
        """
        if not isinstance(self.ratio_qty, int) or isinstance(self.ratio_qty, bool):
            raise ValueError(
                f"ratio_qty must be a whole number of contracts, got "
                f"{self.ratio_qty!r} ({type(self.ratio_qty).__name__})"
            )
        if self.ratio_qty < 1:
            raise ValueError(f"ratio_qty must be >= 1, got {self.ratio_qty}")
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


def allowed_denominations(underlying: str) -> tuple[Structure, ...]:
    """Which denominations this underlying can actually trade.

    European underlyings can only submit same-expiry legs, so K2 - a diagonal
    across T1 and T2 - is rejected outright (HTTP 422, code 42210000, verified
    live on SPX). That leaves T1, which is also the denomination the source
    study singles out as performing best, so the constraint costs nothing.

    American underlyings accept both.
    """
    if underlying.upper() in EUROPEAN_UNDERLYINGS:
        return (Structure.T1,)
    return (Structure.T1, Structure.K2)


class Choice(NamedTuple):
    """A denomination decision, or a decision not to trade.

    A NamedTuple so `choose(...)[0]` keeps working for callers that only want
    the structure, while `.structure is None` expresses abstention - which a
    bare Structure return could not. Abstention is a real outcome, not an error:
    the study's rule is to trade the higher-probability denomination only when
    that probability clears the threshold, and to stand down otherwise.
    """

    structure: Structure | None
    reason: str
    scores: tuple[tuple[str, float], ...] = ()

    @property
    def abstained(self) -> bool:
        return self.structure is None

    @property
    def score_map(self) -> dict[str, float]:
        return dict(self.scores)


class DenominationSelector:
    """Chooses T1 or K2 for a candidate, or abstains.

    Where more than one denomination is submittable the choice is a prediction,
    not a rule: the source study finds both profitable in SPX but with different
    payoff profiles, and the SPY paper selects between them with a trained
    model. This base class is the fallback used when no model is supplied - it
    picks T1, the better-performing denomination in the study, and records that
    the choice was a fallback rather than a prediction.

    `ModelDenominationSelector` is the model-backed implementation.
    """

    name = "default_t1"

    def choose(self, underlying: str, cand=None, underlying_price: float | None = None) -> Choice:
        allowed = allowed_denominations(underlying)
        if len(allowed) == 1:
            return Choice(allowed[0], f"{allowed[0].name} is the only submittable denomination")
        return Choice(Structure.T1, "no model available; defaulting to T1")


class ModelDenominationSelector(DenominationSelector):
    """Selects a denomination from a trained probability model.

    `scorer` maps one live F* feature vector to a probability. It is called
    once per submittable denomination, because a rectangle does not have one
    feature vector - it has one per denomination, differing in exactly the two
    strategy-selected features (F* carries no is_t1/is_k2 column, so that is the
    only channel through which the model learns which trade it is scoring).

    Abstains when the best probability falls below `min_probability`, and also
    when the feature vector cannot be built. The second case matters: a missing
    greek or an unpriceable leg means we do not know what we would be scoring,
    and a selector that guessed T1 there would be presenting an absence of
    information as a prediction.
    """

    name = "model"

    def __init__(self, scorer, min_probability: float = 0.5) -> None:
        self.scorer = scorer
        self.min_probability = min_probability

    def choose(self, underlying: str, cand=None, underlying_price: float | None = None) -> Choice:
        allowed = allowed_denominations(underlying)
        if len(allowed) == 1:
            return Choice(allowed[0], f"{allowed[0].name} is the only submittable denomination")
        if cand is None or underlying_price is None:
            return Choice(None, "no candidate or spot supplied; cannot score a denomination")

        from .features import FeatureError, feature_vector

        scores: list[tuple[str, float]] = []
        for structure in allowed:
            label = structure.name  # "T1" / "K2"
            try:
                vec = feature_vector(cand, label, underlying_price, cand.signal_date)
            except FeatureError as exc:
                return Choice(None, f"features unavailable for {label}: {exc}", tuple(scores))
            scores.append((label, float(self.scorer(vec))))

        best_label, best_p = max(scores, key=lambda kv: kv[1])
        detail = ", ".join(f"{k} {v:.4f}" for k, v in scores)
        if best_p < self.min_probability:
            return Choice(
                None,
                f"best probability {best_p:.4f} is below the {self.min_probability:.2f} "
                f"threshold ({detail}); standing down",
                tuple(scores),
            )
        return Choice(Structure[best_label], f"{best_label} at {best_p:.4f} ({detail})", tuple(scores))


def structure_for(underlying: str, selector: DenominationSelector | None = None,
                  cand=None, underlying_price: float | None = None) -> Structure:
    """Backwards-compatible entry point returning a single structure.

    Raises if the selector abstained: callers of this function have no way to
    represent "no trade", so silently substituting a default would convert a
    stand-down into a position.
    """
    sel = selector or DenominationSelector()
    choice = sel.choose(underlying, cand, underlying_price)
    if choice.structure is None:
        raise ValueError(f"selector abstained: {choice.reason}")
    return choice.structure


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

    if cfg.structure in (Structure.T1, Structure.K2):
        # Both denominations short D. T1 buys A (same expiry as D); K2 buys B.
        if cfg.structure is Structure.T1:
            long_leg, label = A, "T1"
        else:
            long_leg, label = B, "K2"

        debit = long_leg.quote.ask - D.quote.bid
        # If the long strike sits ABOVE the short strike the package can lose
        # more than the debit. That never happens for T1, where K1 < K2~ makes
        # it a bull call spread capped at the debit, but K2 is a diagonal: it
        # buys B at K2 and shorts D at K2~ <= K2. At T1 with S large the short
        # pays (S - K2~) while the long is worth about (S - K2), so the package
        # settles at -(K2 - K2~) on top of the debit.
        #
        # Usually zero - rounding K2~ up to a listed strike lands it back on K2
        # in 96.8% of observed SPY rectangles, which makes K2 a pure calendar
        # spread - but it is $100 per contract when it is not, and a max loss
        # that is understated is the one number a risk cap cannot tolerate.
        strike_gap = max(long_leg.strike - D.strike, 0.0)
        spec.legs = [
            PositionLeg(long_leg.symbol, long_leg.strike,
                        long_leg.expiry.isoformat(), Side.BUY, 1, long_leg.quote.ask),
            PositionLeg(D.symbol, D.strike, D.expiry.isoformat(), Side.SELL, 1, D.quote.bid),
        ]
        spec.entry_cash = -debit * CONTRACT_MULTIPLIER
        spec.max_loss = (max(debit, 0.0) + strike_gap) * CONTRACT_MULTIPLIER
        # For T1 both legs share an expiry and K1 < K2~, so the long lower strike
        # covers the short higher one. For K2 the long is later-dated and higher
        # struck, which also caps the short.
        spec.is_covered = (
            D.strike > long_leg.strike or long_leg.expiry > D.expiry
        )
        spec.notes.append(
            f"{label} denomination capped 1:1: buy {long_leg.symbol}, sell "
            f"{D.symbol}. Defined risk, but the paper's credit-equals-violation "
            f"property does not survive the cap."
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

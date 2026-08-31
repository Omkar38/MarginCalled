"""Theory gate: classify a detected TP2 rectangle against Propositions 2.1-2.2.

A TP2 violation observed in SPY American calls is not automatically interpretable
against the European TP2 benchmark, because an American call carries an
early-exercise premium pi(K, T) >= 0 that need not affect the four contracts of a
rectangle in the same way. This module decides, per rectangle, whether that
premium can be ruled out or bounded.

Four outcomes, evaluated as a cascade:

    NO_DISTRIBUTION     no ex-dividend date in (t0, T2]; Prop 2.1(i) gives pi = 0
                        for every contract, so the rectangle is European-equivalent.

    DIVIDEND_SPANNING   an ex-date falls before T2, but Prop 2.1(ii) holds for all
                        four contracts, so pi = 0 anyway.

    DIVIDEND_BOUND      the zero-premium conditions fail, but the violation is too
                        large to be explained by any admissible premium (Prop 2.2).

    UNRESOLVED          none of the above. The premium enters both sides of the TP2
                        inequality, so its nonnegativity alone does not sign the
                        American-versus-European change in the determinant. Reject.

The propositions are stated for a single signal date taken as time zero, with
deterministic cash distributions and discount factors, frictionless exercise, and
no ownership benefit beyond the stated distributions.

This module has no third-party dependencies by design: it must be runnable in any
environment, including inside a live trading loop, without importing a data stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Iterable, Sequence

__all__ = [
    "Category",
    "classify_european",
    "Dividend",
    "Contract",
    "Rectangle",
    "GateResult",
    "year_fraction",
    "discounted_dividend_total",
    "zero_premium_holds",
    "classify",
]

# Day-count convention. The paper discounts with exp(-r t) where t is in years and
# the discount curve is approximated by the contemporaneous three-month rate.
DAYS_PER_YEAR = 365.0


class Category(str, Enum):
    """Theory categories, in cascade order."""

    # For genuinely European contracts (SPX, XSP, VIX, DJX index options) there is
    # no early-exercise feature at all, so the premium is zero by definition rather
    # than by Proposition 2.1. The European TP2 benchmark applies directly and the
    # American reduction is unnecessary - which is precisely why an index scanner
    # is a clean control against SPY.
    EUROPEAN_NATIVE = "european_native"

    NO_DISTRIBUTION = "no_distribution"
    DIVIDEND_SPANNING = "dividend_spanning"
    DIVIDEND_BOUND = "dividend_bound"
    UNRESOLVED = "unresolved"

    @property
    def is_european_equivalent(self) -> bool:
        """True when Proposition 2.1 certifies a zero early-exercise premium."""
        return self in (
            Category.EUROPEAN_NATIVE,
            Category.NO_DISTRIBUTION,
            Category.DIVIDEND_SPANNING,
        )

    @property
    def is_tradable(self) -> bool:
        """Whether the gate permits the rectangle to proceed downstream.

        UNRESOLVED is rejected: in the paper's own out-of-sample book this category
        carries the great majority of the unconditional loss.
        """
        return self is not Category.UNRESOLVED


@dataclass(frozen=True)
class Dividend:
    """One announced cash distribution.

    Attributes:
        ex_date: the ex-dividend date.
        amount: the cash amount per share.
        announced_on: when the amount became public. Supplied so the gate can refuse
            to use an amount that was not knowable at the signal date. The paper
            keeps dividend fields out of the model features for exactly this reason,
            and admits them only on the theory side.
    """

    ex_date: date
    amount: float
    announced_on: date | None = None

    def known_at(self, as_of: date) -> bool:
        """Whether this amount was public on `as_of`. Unknown provenance is trusted."""
        return self.announced_on is None or self.announced_on <= as_of


@dataclass(frozen=True)
class Contract:
    """One leg of a rectangle: a call at strike `strike` expiring `expiry`."""

    label: str
    strike: float
    expiry: date
    bid: float | None = None
    ask: float | None = None


@dataclass(frozen=True)
class Rectangle:
    """The four contracts of a forward-adjusted TP2 rectangle.

    Labels follow the paper: A = C(K1, T1), B = C(K2, T2), C = C(K1~, T2),
    D = C(K2~, T1). A and B are the undervalued pair; C and D are the overvalued
    pair, and D is the short near-dated leg both retained denominations sell.
    """

    signal_date: date
    A: Contract
    B: Contract
    C: Contract
    D: Contract

    @property
    def contracts(self) -> tuple[Contract, Contract, Contract, Contract]:
        return (self.A, self.B, self.C, self.D)

    @property
    def T1(self) -> date:
        """Near maturity. A and D expire here."""
        return self.A.expiry

    @property
    def T2(self) -> date:
        """Far maturity. B and C expire here."""
        return self.B.expiry


@dataclass
class GateResult:
    """Outcome of the gate, shaped for the audit log and the narrator."""

    category: Category
    reasons: list[str] = field(default_factory=list)
    per_contract_zero_premium: dict[str, bool] = field(default_factory=dict)
    dividends_used: list[Dividend] = field(default_factory=list)
    dbar_T1: float = 0.0
    dbar_T2: float = 0.0
    violation_size: float | None = None
    bound_M: float | None = None

    @property
    def is_tradable(self) -> bool:
        return self.category.is_tradable

    def to_record(self) -> dict:
        """Flat, JSON-serialisable record for the append-only audit log."""
        return {
            "category": self.category.value,
            "is_european_equivalent": self.category.is_european_equivalent,
            "is_tradable": self.is_tradable,
            "reasons": list(self.reasons),
            "per_contract_zero_premium": dict(self.per_contract_zero_premium),
            "dividends_used": [
                {"ex_date": d.ex_date.isoformat(), "amount": d.amount}
                for d in self.dividends_used
            ],
            "dbar_T1": self.dbar_T1,
            "dbar_T2": self.dbar_T2,
            "violation_size": self.violation_size,
            "bound_M": self.bound_M,
        }


def year_fraction(start: date, end: date, days_per_year: float = DAYS_PER_YEAR) -> float:
    """Signed year fraction between two dates."""
    return (end - start).days / days_per_year


def _usable_dividends(
    dividends: Iterable[Dividend], signal_date: date, horizon: date
) -> list[Dividend]:
    """Announced dividends with an ex-date in (signal_date, horizon], sorted."""
    kept = [
        d
        for d in dividends
        if signal_date < d.ex_date <= horizon and d.known_at(signal_date)
    ]
    return sorted(kept, key=lambda d: d.ex_date)


def discounted_dividend_total(
    dividends: Sequence[Dividend], signal_date: date, horizon: date, r: float
) -> float:
    """Dbar(T) = sum over ex-dates t_i <= T of delta_i * exp(-r * t_i).

    This is the bound on the early-exercise premium used by Proposition 2.2.
    """
    total = 0.0
    for d in _usable_dividends(dividends, signal_date, horizon):
        t_i = year_fraction(signal_date, d.ex_date)
        total += d.amount * math.exp(-r * t_i)
    return total


def zero_premium_holds(
    strike: float,
    expiry: date,
    dividends: Sequence[Dividend],
    signal_date: date,
    r: float,
) -> bool:
    """Proposition 2.1: does this contract have a zero early-exercise premium?

    Part (i): no ex-dividend date in (t0, T] means early exercise is never optimal,
    by the standard no-dividend argument, so pi = 0.

    Part (ii): at every ex-date t_i <= T,

        delta_i + sum_{j>i, t_j<=T} delta_j exp(-r (t_j - t_i))
            <= K (1 - exp(-r (T - t_i))).

    The left side is the distribution forgone by not exercising; the right side is
    the interest earned by deferring payment of the strike. When the inequality
    holds at every ex-date, immediate exercise is never strictly better and pi = 0.

    Note this is monotone in r: higher rates make the condition easier to satisfy.
    At r ~ 0 the right side vanishes and the condition fails for any contract that
    spans an ex-date, which is why the European-equivalent share collapses in a
    zero-rate environment.
    """
    relevant = _usable_dividends(dividends, signal_date, expiry)
    if not relevant:
        return True  # Part (i)

    for i, div_i in enumerate(relevant):
        t_i = year_fraction(signal_date, div_i.ex_date)
        T = year_fraction(signal_date, expiry)

        forgone = div_i.amount
        for div_j in relevant[i + 1 :]:
            t_j = year_fraction(signal_date, div_j.ex_date)
            forgone += div_j.amount * math.exp(-r * (t_j - t_i))

        deferral_benefit = strike * (1.0 - math.exp(-r * (T - t_i)))

        if forgone > deferral_benefit:
            return False  # Early exercise may be optimal at t_i.

    return True


def classify_european(rect: Rectangle) -> GateResult:
    """Classify a rectangle on a genuinely European underlying.

    No early-exercise feature exists, so pi = 0 identically and Propositions
    2.1-2.2 have nothing to do. Returned as its own category rather than folded
    into NO_DISTRIBUTION so the two cases stay distinguishable in the audit log:
    one is certified by a theorem, the other by the contract specification.
    """
    return GateResult(
        category=Category.EUROPEAN_NATIVE,
        reasons=[
            "European-style contract: no early-exercise feature, so the premium is "
            "zero by definition and the European TP2 benchmark applies directly."
        ],
        per_contract_zero_premium={c.label: True for c in rect.contracts},
    )


def classify(
    rect: Rectangle,
    dividends: Sequence[Dividend],
    r: float,
    violation_size: float | None = None,
) -> GateResult:
    """Classify a rectangle into one of the four theory categories.

    Args:
        rect: the four contracts and the signal date.
        dividends: announced distributions. Amounts not public as of the signal date
            are discarded rather than trusted.
        r: continuously compounded short rate.
        violation_size: V^q = C^b D^b - A^a B^a, the strong-violation size measured
            on executable quote sides. Required only to test Proposition 2.2; when
            omitted, a rectangle that fails Proposition 2.1 returns UNRESOLVED.

    Returns:
        A GateResult carrying the category and the evidence behind it.
    """
    result = GateResult(category=Category.UNRESOLVED, violation_size=violation_size)

    usable = _usable_dividends(dividends, rect.signal_date, rect.T2)
    result.dividends_used = usable

    discarded = [
        d
        for d in dividends
        if rect.signal_date < d.ex_date <= rect.T2 and not d.known_at(rect.signal_date)
    ]
    if discarded:
        result.reasons.append(
            f"{len(discarded)} distribution(s) discarded: amount not announced as of "
            f"{rect.signal_date.isoformat()}"
        )

    # --- Proposition 2.1(i): no distribution before the far maturity -------------
    if not usable:
        result.category = Category.NO_DISTRIBUTION
        result.reasons.append(
            f"No ex-dividend date in ({rect.signal_date.isoformat()}, "
            f"{rect.T2.isoformat()}]; Prop 2.1(i) gives zero early-exercise premium "
            f"for all four contracts."
        )
        result.per_contract_zero_premium = {c.label: True for c in rect.contracts}
        return result

    # --- Proposition 2.1(ii): zero premium at every ex-date, all four legs -------
    per_contract = {
        c.label: zero_premium_holds(c.strike, c.expiry, dividends, rect.signal_date, r)
        for c in rect.contracts
    }
    result.per_contract_zero_premium = per_contract

    if all(per_contract.values()):
        result.category = Category.DIVIDEND_SPANNING
        result.reasons.append(
            f"{len(usable)} distribution(s) before T2, but Prop 2.1(ii) holds for all "
            f"four contracts at r={r:.4f}; early exercise is never strictly optimal."
        )
        return result

    failed = sorted(label for label, ok in per_contract.items() if not ok)
    result.reasons.append(
        f"Prop 2.1(ii) fails for leg(s) {', '.join(failed)} at r={r:.4f}."
    )

    # --- Proposition 2.2: is the violation too large to be early exercise? -------
    result.dbar_T1 = discounted_dividend_total(dividends, rect.signal_date, rect.T1, r)
    result.dbar_T2 = discounted_dividend_total(dividends, rect.signal_date, rect.T2, r)

    if violation_size is None:
        result.reasons.append(
            "Violation size not supplied; Prop 2.2 certificate not evaluated."
        )
        return result

    if rect.C.bid is None or rect.D.bid is None:
        result.reasons.append(
            "Bid quotes for legs C and D required for Prop 2.2; not supplied."
        )
        return result

    # M = Dbar_2 * D^b + Dbar_1 * C^b, per the certificate. The omitted cross term
    # Dbar_1 * Dbar_2 >= 0 only strengthens the bound, so dropping it is conservative.
    bound_M = result.dbar_T2 * rect.D.bid + result.dbar_T1 * rect.C.bid
    result.bound_M = bound_M

    if violation_size > bound_M:
        result.category = Category.DIVIDEND_BOUND
        result.reasons.append(
            f"Prop 2.2 certificate holds: V^q={violation_size:.6f} > M={bound_M:.6f}; "
            f"the early-exercise premium cannot explain the observed inequality."
        )
        return result

    result.reasons.append(
        f"Prop 2.2 fails: V^q={violation_size:.6f} <= M={bound_M:.6f}. "
        f"Early exercise could account for the violation. Rejected."
    )
    return result

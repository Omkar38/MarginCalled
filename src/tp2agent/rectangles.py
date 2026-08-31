"""Rectangle builder and quote-side strong-violation test.

Turns one option-chain snapshot into scored candidate TP2 rectangles.

The TP2 benchmark for European calls requires, for K1 <= K2 and T1 <= T2,

    C(K1,T1) C(K2,T2) >= C(K1,T2) C(K2,T1).

With interest rates and dividends the comparison aligns strikes by forward
moneyness, so the empirical form uses forward-adjusted strikes

    K1~ = K1 * F_T2 / F_T1,      K2~ = K2 * F_T1 / F_T2,

each rounded up to the next listed strike. Labelling the four contracts

    A = C(K1, T1)    B = C(K2, T2)    C = C(K1~, T2)    D = C(K2~, T1)

a violation reverses the inequality. A *strong* violation also survives the quoted
spread: it is measured on the sides a trade would actually cross,

    A^ask * B^ask  <  C^bid * D^bid,

which is conservative with respect to the spread, though it does not guarantee
simultaneous execution at all four displayed prices.

Two practical guards this module adds beyond the arithmetic:

  Tick quantisation. The test compares *products* of four quoted prices. At a
  $0.01 tick, each factor carries up to half a tick of error, so the product
  carries a relative error roughly equal to the sum of the per-leg relative
  errors. On cheap contracts that is large enough to manufacture violations out
  of rounding alone. `tick_error_bound` computes it per rectangle and the
  detector requires the observed violation to clear it.

  Coverage ratio. The position weights are B_w = price(B), C_w = price(C). Since
  K1~ < K2 at the same maturity T2, C sits at the lower strike and is therefore
  always the dearer contract, so C_w > B_w on every rectangle: the short leg
  always outnumbers the long leg. Alpaca rejects uncovered short legs inside a
  multi-leg order, so the executed position must cap shorts at longs. That
  distortion is small only when price(C)/price(B) is near 1, so the ratio is
  emitted here and screened before anything downstream runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

__all__ = [
    "vertical_arbitrage_free",
    "Quote",
    "OptionQuote",
    "ChainSnapshot",
    "RectangleConfig",
    "RectangleCandidate",
    "implied_forward",
    "round_up_to_listed",
    "tick_error_bound",
    "build_rectangles",
]

DAYS_PER_YEAR = 365.0
DEFAULT_TICK = 0.01


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def relative_spread(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else float("inf")

    @property
    def is_usable(self) -> bool:
        """Positive bid, uncrossed, and displayed size on both sides."""
        return (
            self.bid > 0
            and self.ask > self.bid
            and self.bid_size > 0
            and self.ask_size > 0
        )


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    strike: float
    expiry: date
    right: str  # "C" or "P"
    quote: Quote


@dataclass
class ChainSnapshot:
    """One point-in-time view of the chain, keyed by expiry then strike."""

    asof: date
    underlying_price: float
    calls: dict[date, dict[float, OptionQuote]] = field(default_factory=dict)
    puts: dict[date, dict[float, OptionQuote]] = field(default_factory=dict)

    collisions: int = 0

    def add(self, opt: OptionQuote) -> None:
        """Insert a quote, counting any (expiry, strike) collision.

        Two contracts can share an expiry date and strike but differ in settlement
        (AM-settled monthlies vs PM-settled weeklies on an index). Silently
        overwriting one with the other would build rectangles from mixed
        settlement types, which the source study explicitly avoids. Alpaca has not
        been observed to return such pairs, so this counts rather than raises -
        but it must never pass unnoticed.
        """
        book = self.calls if opt.right == "C" else self.puts
        by_strike = book.setdefault(opt.expiry, {})
        if opt.strike in by_strike and by_strike[opt.strike].symbol != opt.symbol:
            self.collisions += 1
        by_strike[opt.strike] = opt

    def expiries(self) -> list[date]:
        return sorted(self.calls)

    def listed_strikes(self, expiry: date) -> list[float]:
        return sorted(self.calls.get(expiry, {}))


@dataclass(frozen=True)
class RectangleConfig:
    """Screens applied while building. Every threshold is logged, not implicit."""

    # Quote quality
    max_relative_spread: float = 0.50
    min_size: float = 1.0
    # Below this mid, half a tick is a large fraction of the price and the
    # four-way product carries error of the same order as any real signal. These
    # are also the contracts where the source study found costs dominate: its
    # median gross entry exposure was $7-11 across two legs.
    min_leg_mid: float = 0.50

    # Universe
    min_moneyness: float = 0.90  # K / F bounds
    max_moneyness: float = 1.10
    max_strike_gap_pct: float = 0.06  # (K2 - K1) / F

    # Detection
    violation_buffer_pct: float = 0.02  # of C^b D^b, on top of the tick bound
    tick: float = DEFAULT_TICK

    # Coverage (Finding 1): reject wide-gap rectangles where the forced 1:1 cap
    # would badly distort the intended position.
    max_coverage_ratio: float = 1.25

    # Rounding proximity. Glasserman, Li & Pirjol (2025) sec. 4.1: "if no strike
    # is traded within $50 of the rounded-up strike, we discard this option as it
    # indicates that this particular strike is in an illiquid region where few
    # neighboring contracts are traded." Expressed here as a fraction of the
    # forward so it transfers across underlyings ($50 on a ~6000 index is ~0.83%).
    max_strike_roundup_pct: float = 0.01

    # Forward estimation
    min_parity_strikes: int = 5
    parity_moneyness_band: float = 0.05


@dataclass
class RectangleCandidate:
    """A detected strong violation, with the evidence behind it."""

    signal_date: date
    T1: date
    T2: date
    K1: float
    K2: float
    K1_adj: float
    K2_adj: float
    A: OptionQuote
    B: OptionQuote
    C: OptionQuote
    D: OptionQuote
    F_T1: float
    F_T2: float
    lhs: float  # A^ask * B^ask
    rhs: float  # C^bid * D^bid
    violation_size: float  # V^q = rhs - lhs
    tick_bound: float
    coverage_ratio: float  # price(C) / price(B) == C_w / B_w

    @property
    def normalized_severity(self) -> float:
        return self.violation_size / self.rhs if self.rhs > 0 else 0.0

    @property
    def episode_key(self) -> str:
        """Dedup key: the near-leg short contract, per the paper's convention."""
        return self.D.symbol

    def to_record(self) -> dict:
        return {
            "signal_date": self.signal_date.isoformat(),
            "T1": self.T1.isoformat(),
            "T2": self.T2.isoformat(),
            "K1": self.K1,
            "K2": self.K2,
            "K1_adj": self.K1_adj,
            "K2_adj": self.K2_adj,
            "symbols": {
                "A": self.A.symbol,
                "B": self.B.symbol,
                "C": self.C.symbol,
                "D": self.D.symbol,
            },
            "F_T1": self.F_T1,
            "F_T2": self.F_T2,
            "lhs_ask_product": self.lhs,
            "rhs_bid_product": self.rhs,
            "violation_size": self.violation_size,
            "normalized_severity": self.normalized_severity,
            "tick_bound": self.tick_bound,
            "coverage_ratio": self.coverage_ratio,
            "episode_key": self.episode_key,
        }


def year_fraction(start: date, end: date) -> float:
    return (end - start).days / DAYS_PER_YEAR


def implied_forward(
    chain: ChainSnapshot, expiry: date, r: float, cfg: RectangleConfig
) -> float | None:
    """Parity-implied forward, estimated cross-sectionally.

    For each strike with a usable call and put, put-call parity gives

        F = K + exp(r * T) * (C_mid - P_mid).

    Estimates are taken across strikes near the money and reduced by the median,
    so a single bad quote cannot move the result. Returns None when too few
    strikes qualify, which is a refusal rather than a guess: every downstream
    quantity depends on this number.
    """
    calls = chain.calls.get(expiry, {})
    puts = chain.puts.get(expiry, {})
    if not calls or not puts:
        return None

    T = year_fraction(chain.asof, expiry)
    if T <= 0:
        return None
    carry = math.exp(r * T)

    spot = chain.underlying_price
    lo = spot * (1.0 - cfg.parity_moneyness_band)
    hi = spot * (1.0 + cfg.parity_moneyness_band)

    estimates: list[float] = []
    for strike in sorted(set(calls) & set(puts)):
        if not (lo <= strike <= hi):
            continue
        c, p = calls[strike], puts[strike]
        if not (c.quote.is_usable and p.quote.is_usable):
            continue
        estimates.append(strike + carry * (c.quote.mid - p.quote.mid))

    if len(estimates) < cfg.min_parity_strikes:
        return None

    estimates.sort()
    n = len(estimates)
    if n % 2:
        return estimates[n // 2]
    return 0.5 * (estimates[n // 2 - 1] + estimates[n // 2])


def round_up_to_listed(target: float, listed: Sequence[float]) -> float | None:
    """Round a forward-adjusted strike up to the next listed strike."""
    for strike in listed:  # assumed sorted ascending
        if strike >= target - 1e-9:
            return strike
    return None


def tick_error_bound(quotes: Iterable[Quote], tick: float = DEFAULT_TICK) -> float:
    """Worst-case relative error in a product of quoted prices from quantisation.

    Each quoted price carries up to half a tick of error. The product's worst-case
    relative deviation is therefore

        prod(1 + h/p_i) - 1,     h = tick/2,

    taken exactly rather than by the first-order sum(h/p_i). The two agree to
    first order, but the sum *under*-states the true worst case (by ~0.75% on
    dollar-priced legs, more as legs get cheaper), and a bound used to reject
    marginal detections must err high, never low. The exact form is superadditive
    in the number of legs, which is the correct behaviour.

    On cheap near-expiry contracts this is large - exactly where a naive
    determinant test invents violations out of rounding.
    """
    factor = 1.0
    half = 0.5 * tick
    for q in quotes:
        price = max(q.mid, tick)
        factor *= 1.0 + half / price
    return factor - 1.0


def vertical_arbitrage_free(
    low: OptionQuote, high: OptionQuote, tol: float = 1e-9
) -> bool:
    """Do two same-expiry calls respect the vertical no-arbitrage bound?

    For calls at a shared maturity with K_low < K_high,

        C(K_low) - C(K_high) <= K_high - K_low,

    since the spread can never be worth more than its width. Measured on the sides
    a trade would actually cross, the credit collected by selling the low strike
    and buying the high one must not exceed the width:

        bid(K_low) - ask(K_high) <= K_high - K_low.

    A breach is free money on a plain two-leg vertical - simpler, larger, and more
    certain than any TP2 trade - which means the quotes are broken, not that an
    opportunity exists. Rectangles containing such a pair must be discarded before
    the TP2 test, or the determinant merely inherits the inconsistency and reports
    it as a surface anomaly.
    """
    if high.strike < low.strike:
        low, high = high, low
    width = high.strike - low.strike
    credit = low.quote.bid - high.quote.ask
    return credit <= width + tol


def build_rectangles(
    chain: ChainSnapshot,
    r: float,
    cfg: RectangleConfig | None = None,
    margins: list[float] | None = None,
) -> tuple[list[RectangleCandidate], dict[str, int]]:
    """Build all rectangles from a snapshot and return strong violations.

    Returns the surviving candidates and a census of why the others were dropped.
    The census is the number to watch on a shadow run: it says whether the
    scanner is finding nothing because the market is clean or because a screen is
    mis-set.

    If `margins` is supplied, the normalized TP2 margin (rhs - lhs) / rhs is
    appended for every rectangle that reaches the violation test, whether or not
    it violates. A positive value is a violation. The distribution of these is how
    you tell a clean market from a synthetic one: real quotes produce near-zero
    margins, a fitted arbitrage-free surface never does.
    """
    cfg = cfg or RectangleConfig()
    census: dict[str, int] = {
        "expiry_pairs": 0,
        "rectangles_considered": 0,
        "no_forward": 0,
        "adjusted_strike_unlisted": 0,
        "leg_missing": 0,
        "leg_unusable": 0,
        "strike_gap_too_wide": 0,
        "coverage_ratio_too_wide": 0,
        "roundup_too_far": 0,
        "leg_too_cheap": 0,
        "degenerate_legs": 0,
        "vertical_arbitrage": 0,
        "no_violation": 0,
        "below_tick_bound": 0,
        "detected": 0,
    }

    expiries = chain.expiries()
    forwards: dict[date, float | None] = {
        e: implied_forward(chain, e, r, cfg) for e in expiries
    }

    out: list[RectangleCandidate] = []

    for i, T1 in enumerate(expiries):
        for T2 in expiries[i + 1 :]:
            F1, F2 = forwards.get(T1), forwards.get(T2)
            if F1 is None or F2 is None or F1 <= 0 or F2 <= 0:
                census["no_forward"] += 1
                continue
            census["expiry_pairs"] += 1

            listed_T1 = chain.listed_strikes(T1)
            listed_T2 = chain.listed_strikes(T2)
            if not listed_T1 or not listed_T2:
                continue

            candidates_K1 = [
                k for k in listed_T1 if cfg.min_moneyness <= k / F1 <= cfg.max_moneyness
            ]
            candidates_K2 = [
                k for k in listed_T2 if cfg.min_moneyness <= k / F2 <= cfg.max_moneyness
            ]

            for K1 in candidates_K1:
                for K2 in candidates_K2:
                    # Ordering condition of the empirical benchmark.
                    if K1 / F1 >= K2 / F2:
                        continue
                    census["rectangles_considered"] += 1

                    if (K2 - K1) / F1 > cfg.max_strike_gap_pct:
                        census["strike_gap_too_wide"] += 1
                        continue

                    exact_1 = K1 * F2 / F1
                    exact_2 = K2 * F1 / F2
                    K1_adj = round_up_to_listed(exact_1, listed_T2)
                    K2_adj = round_up_to_listed(exact_2, listed_T1)
                    if K1_adj is None or K2_adj is None:
                        census["adjusted_strike_unlisted"] += 1
                        continue

                    # The rounded-up strike must actually be near the exact one.
                    # A distant next-listed strike means an illiquid region of the
                    # board, and the rounding would move the comparison far from
                    # the rectangle the theory specifies.
                    if (
                        K1_adj - exact_1 > cfg.max_strike_roundup_pct * F2
                        or K2_adj - exact_2 > cfg.max_strike_roundup_pct * F1
                    ):
                        census["roundup_too_far"] += 1
                        continue

                    A = chain.calls[T1].get(K1)
                    B = chain.calls[T2].get(K2)
                    C = chain.calls[T2].get(K1_adj)
                    D = chain.calls[T1].get(K2_adj)
                    if not (A and B and C and D):
                        census["leg_missing"] += 1
                        continue

                    # The four legs must be four *distinct* contracts. When K2 and
                    # K1~ (or K1 and K2~) round to the same listed strike at the
                    # same maturity, B and C collapse into one contract and the
                    # determinant degenerates into comparing a contract against its
                    # own spread - which is not a TP2 test at all.
                    legs = (A, B, C, D)
                    if len({leg.symbol for leg in legs}) < 4:
                        census["degenerate_legs"] += 1
                        continue

                    if any(leg.quote.mid < cfg.min_leg_mid for leg in legs):
                        census["leg_too_cheap"] += 1
                        continue

                    if not all(leg.quote.is_usable for leg in legs):
                        census["leg_unusable"] += 1
                        continue
                    if any(
                        leg.quote.relative_spread > cfg.max_relative_spread
                        for leg in legs
                    ):
                        census["leg_unusable"] += 1
                        continue
                    if any(
                        min(leg.quote.bid_size, leg.quote.ask_size) < cfg.min_size
                        for leg in legs
                    ):
                        census["leg_unusable"] += 1
                        continue

                    # Quotes must respect the vertical bound at each maturity
                    # before the TP2 determinant means anything. Without this, a
                    # broken same-expiry pair propagates into the product and is
                    # reported as a surface anomaly.
                    if not (
                        vertical_arbitrage_free(A, D)
                        and vertical_arbitrage_free(C, B)
                    ):
                        census["vertical_arbitrage"] += 1
                        continue

                    # Coverage ratio (Finding 1). C is the dearer leg by
                    # construction; screen before doing more work.
                    coverage = (
                        C.quote.mid / B.quote.mid if B.quote.mid > 0 else float("inf")
                    )
                    if coverage > cfg.max_coverage_ratio:
                        census["coverage_ratio_too_wide"] += 1
                        continue

                    lhs = A.quote.ask * B.quote.ask
                    rhs = C.quote.bid * D.quote.bid
                    if margins is not None and rhs > 0:
                        margins.append((rhs - lhs) / rhs)
                    if rhs <= lhs:
                        census["no_violation"] += 1
                        continue

                    violation = rhs - lhs
                    tick_bound = tick_error_bound(
                        [leg.quote for leg in legs], cfg.tick
                    )
                    required = rhs * (tick_bound + cfg.violation_buffer_pct)
                    if violation <= required:
                        census["below_tick_bound"] += 1
                        continue

                    census["detected"] += 1
                    out.append(
                        RectangleCandidate(
                            signal_date=chain.asof,
                            T1=T1,
                            T2=T2,
                            K1=K1,
                            K2=K2,
                            K1_adj=K1_adj,
                            K2_adj=K2_adj,
                            A=A,
                            B=B,
                            C=C,
                            D=D,
                            F_T1=F1,
                            F_T2=F2,
                            lhs=lhs,
                            rhs=rhs,
                            violation_size=violation,
                            tick_bound=tick_bound,
                            coverage_ratio=coverage,
                        )
                    )

    out.sort(key=lambda c: c.normalized_severity, reverse=True)
    return out, census


def dedupe_episodes(
    candidates: Sequence[RectangleCandidate],
) -> list[RectangleCandidate]:
    """Keep the most severe candidate per near-leg contract.

    The same violation reappears across neighbouring rectangles that share the
    short near-dated leg. Counting those as independent would over-weight one
    market event, so they are consolidated, following the paper's convention of
    keying episodes on the near-leg contract.
    """
    best: dict[str, RectangleCandidate] = {}
    for cand in candidates:
        key = cand.episode_key
        if key not in best or cand.normalized_severity > best[key].normalized_severity:
            best[key] = cand
    return sorted(best.values(), key=lambda c: c.normalized_severity, reverse=True)

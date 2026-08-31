"""Mathematical audit.

TP2 is a property of EUROPEAN call prices. Everything downstream rests on that,
so this file verifies the identities the implementation assumes, rather than the
behaviour the other test files cover.

Each test states the claim it checks and fails loudly if the algebra does not
hold. Where a closed form exists it is compared against the code numerically over
a grid, not at a single point.
"""

from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.rectangles import (  # noqa: E402
    DEFAULT_TICK,
    Quote,
    round_up_to_listed,
    tick_error_bound,
)
from tp2agent.theory_gate import (  # noqa: E402
    Dividend,
    discounted_dividend_total,
    zero_premium_holds,
)

SIGNAL = date(2026, 8, 31)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(F: float, K: float, T: float, vol: float, r: float = 0.0) -> float:
    """Black-76: undiscounted call on a forward. TP2 is stated on such prices."""
    if T <= 0 or vol <= 0:
        return max(F - K, 0.0)
    v = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    return F * _ncdf(d1) - K * _ncdf(d1 - v)


# ==========================================================================
# 1. The forward adjustment
# ==========================================================================


def test_forward_adjustment_preserves_moneyness():
    """K1~ / F_T2 == K1 / F_T1  and  K2~ / F_T1 == K2 / F_T2.

    This is the whole point of the adjustment: it re-expresses each strike at the
    other maturity so both are compared at equal forward moneyness. Without this
    identity the rectangle is not a TP2 rectangle.
    """
    for F1, F2, K1, K2 in product(
        (100.0, 640.0, 7650.0), (101.0, 645.0, 7700.0), (95.0, 0.98), (1.02, 1.05)
    ):
        k1 = K1 * F1 if K1 < 10 else K1
        k2 = K2 * F2
        k1_adj = k1 * F2 / F1
        k2_adj = k2 * F1 / F2
        assert abs(k1_adj / F2 - k1 / F1) < 1e-12
        assert abs(k2_adj / F1 - k2 / F2) < 1e-12


def test_ordering_condition_implies_strike_interleaving():
    """K1/F1 < K2/F2  =>  K1~ < K2  AND  K1 < K2~.

    Finding 1 (the short-heavy coverage result) rests entirely on K1~ < K2: it is
    what makes C the lower strike at the shared maturity T2, hence the dearer leg,
    hence C_w > B_w on every rectangle.
    """
    for F1, F2 in ((640.0, 645.0), (7650.0, 7700.0), (100.0, 100.5)):
        for m1 in (0.90, 0.97, 1.00):
            for m2 in (m1 + 0.005, m1 + 0.05, m1 + 0.15):
                K1, K2 = m1 * F1, m2 * F2
                assert K1 / F1 < K2 / F2  # premise
                assert K1 * F2 / F1 < K2, "K1~ < K2 must follow"
                assert K1 < K2 * F1 / F2, "K1 < K2~ must follow"


def test_C_is_always_the_dearer_leg_at_T2():
    """Since K1~ < K2 at the shared maturity T2, price(C) > price(B) always."""
    F1, F2, T1, T2, vol = 640.0, 645.0, 0.12, 0.25, 0.18
    for m1 in (0.92, 0.97, 1.00, 1.03):
        for m2 in (m1 + 0.01, m1 + 0.05):
            K1, K2 = m1 * F1, m2 * F2
            K1_adj = K1 * F2 / F1
            price_C = bs_call(F2, K1_adj, T2, vol)
            price_B = bs_call(F2, K2, T2, vol)
            assert price_C > price_B, (m1, m2, price_C, price_B)


# ==========================================================================
# 2. TP2 itself
# ==========================================================================


def test_tp2_equals_ratio_monotonicity():
    """A*B >= C*D  <=>  C(m2,T2)/C(m1,T2) >= C(m2,T1)/C(m1,T1).

    The determinant form and the paper's verbal statement - "the price ratio of a
    higher-strike call to a lower-strike call increases with maturity" - must be
    the same claim.
    """
    F1, F2, vol = 640.0, 645.0, 0.18
    T1, T2 = 0.12, 0.25
    for m1 in (0.94, 0.98, 1.02):
        for m2 in (m1 + 0.02, m1 + 0.06):
            A = bs_call(F1, m1 * F1, T1, vol)
            B = bs_call(F2, m2 * F2, T2, vol)
            C = bs_call(F2, m1 * F2, T2, vol)
            D = bs_call(F1, m2 * F1, T1, vol)
            determinant = A * B >= C * D
            ratio = (B / C) >= (D / A)
            assert determinant == ratio, (m1, m2)


def test_black_scholes_satisfies_tp2_everywhere():
    """Black-Scholes call prices are TP2. Any violation here is a code error.

    Checked over a grid of moneyness pairs, maturity pairs and volatilities, in
    normalized (forward-moneyness) coordinates where TP2 is stated.
    """
    worst = 1.0
    for vol in (0.10, 0.18, 0.35, 0.60):
        for T1, T2 in ((0.05, 0.10), (0.12, 0.25), (0.25, 1.00), (0.5, 2.0)):
            for m1 in (0.80, 0.90, 0.95, 1.00, 1.05, 1.15):
                for m2 in (m1 * 1.01, m1 * 1.05, m1 * 1.20):
                    A = bs_call(1.0, m1, T1, vol)
                    B = bs_call(1.0, m2, T2, vol)
                    C = bs_call(1.0, m1, T2, vol)
                    D = bs_call(1.0, m2, T1, vol)
                    if C * D <= 0:
                        continue
                    ratio = (A * B) / (C * D)
                    worst = min(worst, ratio)
                    assert ratio >= 1.0 - 1e-9, (vol, T1, T2, m1, m2, ratio)
    assert worst >= 1.0 - 1e-9


def test_rounding_up_is_conservative():
    """Rounding both adjusted strikes UP can only shrink the right-hand side.

    rhs = C(K1~,T2) * C(K2~,T1). Calls are decreasing in strike, so rounding each
    adjusted strike up lowers both factors, making a violation harder to declare,
    never easier. The screen therefore cannot manufacture detections.
    """
    F1, F2, vol, T1, T2 = 640.0, 645.0, 0.18, 0.12, 0.25
    listed = [600.0 + 5.0 * i for i in range(30)]
    K1, K2 = 632.0, 641.0
    exact_1, exact_2 = K1 * F2 / F1, K2 * F1 / F2
    up_1 = round_up_to_listed(exact_1, listed)
    up_2 = round_up_to_listed(exact_2, listed)
    assert up_1 >= exact_1 and up_2 >= exact_2
    rhs_exact = bs_call(F2, exact_1, T2, vol) * bs_call(F1, exact_2, T1, vol)
    rhs_round = bs_call(F2, up_1, T2, vol) * bs_call(F1, up_2, T1, vol)
    assert rhs_round <= rhs_exact + 1e-12


# ==========================================================================
# 3. Put-call parity forward
# ==========================================================================


def test_parity_forward_roundtrips():
    """F = K + exp(rT)(C - P) must recover the forward used to price them."""
    for F, T, r, vol in product((640.0, 7650.0), (0.08, 0.5), (0.0, 0.045), (0.18,)):
        for K in (F * 0.95, F, F * 1.05):
            call = bs_call(F, K, T, vol) * math.exp(-r * T)
            put = call - math.exp(-r * T) * (F - K)  # parity
            recovered = K + math.exp(r * T) * (call - put)
            assert abs(recovered - F) < 1e-8, (F, K, T, r, recovered)


# ==========================================================================
# 4. Proposition 2.1(ii)
# ==========================================================================


def test_prop_21_break_even_matches_closed_form():
    """Single distribution: the condition flips at T - t_i = -ln(1 - d/K)/r.

    d + 0 <= K(1 - exp(-r(T-t_i)))  <=>  T - t_i >= -ln(1 - d/K)/r.
    Verified against the implementation over a grid of ratios and rates.
    """
    ex = date(2026, 9, 18)
    K = 100.0
    for ratio in (0.001, 0.003, 0.01, 0.03):
        for r in (0.01, 0.02, 0.05, 0.08):
            d = ratio * K
            closed_days = -math.log(1.0 - ratio) / r * 365.0
            below = int(math.floor(closed_days)) - 1
            above = int(math.ceil(closed_days)) + 1
            divs = [Dividend(ex, d)]
            assert not zero_premium_holds(
                K, ex + timedelta(days=max(below, 0)), divs, SIGNAL, r
            ), (ratio, r, below)
            assert zero_premium_holds(
                K, ex + timedelta(days=above), divs, SIGNAL, r
            ), (ratio, r, above)


def test_prop_21_is_monotone_in_rate_and_horizon():
    """The condition eases as r rises and as the horizon lengthens."""
    ex, K, divs = date(2026, 9, 18), 100.0, [Dividend(date(2026, 9, 18), 0.3)]
    horizon = ex + timedelta(days=40)
    holds = [zero_premium_holds(K, horizon, divs, SIGNAL, r)
             for r in (0.005, 0.01, 0.02, 0.04, 0.08)]
    assert holds == sorted(holds), f"not monotone in r: {holds}"
    r = 0.02
    holds_h = [zero_premium_holds(K, ex + timedelta(days=d), divs, SIGNAL, r)
               for d in (5, 20, 55, 120, 400)]
    assert holds_h == sorted(holds_h), f"not monotone in horizon: {holds_h}"


def test_multi_dividend_condition_holds_at_every_ex_date():
    """Prop 2.1(ii) is a conjunction over ALL ex-dates, not just the first.

    The forgone sum at t_i discounts later distributions back to t_i (not to t0),
    and the condition must hold at every ex-date t_i <= T. A later ex-date sitting
    close to expiry has little deferral benefit left and is often the binding one -
    which is exactly the case constructed here.
    """
    ex1, ex2 = date(2026, 9, 18), date(2026, 12, 18)
    K, r, d = 100.0, 0.03, 0.3
    divs = [Dividend(ex1, d), Dividend(ex2, d)]
    T = date(2027, 1, 15)

    def at(ti_date, later_dates):
        ti = (ti_date - SIGNAL).days / 365.0
        tT = (T - SIGNAL).days / 365.0
        forgone = d + sum(
            d * math.exp(-r * ((tj - SIGNAL).days / 365.0 - ti)) for tj in later_dates
        )
        return forgone <= K * (1.0 - math.exp(-r * (tT - ti)))

    per_date = [at(ex1, [ex2]), at(ex2, [])]
    assert per_date[0] is True, "first ex-date should hold here"
    assert per_date[1] is False, "second ex-date should bind here"
    assert zero_premium_holds(K, T, divs, SIGNAL, r) == all(per_date)
    assert zero_premium_holds(K, T, divs, SIGNAL, r) is False


# ==========================================================================
# 5. Proposition 2.2
# ==========================================================================


def test_prop_22_bound_expansion_is_exact():
    """(C^b - Dbar2)(D^b - Dbar1) == C^b D^b - M + Dbar1 Dbar2, M = Dbar2 D^b + Dbar1 C^b.

    The certificate drops the cross term Dbar1*Dbar2 >= 0, which only strengthens
    the lower bound - so the test is conservative, never optimistic.
    """
    for cb, db, d1, d2 in product((2.0, 400.0), (1.5, 380.0), (0.0, 1.79), (0.9, 3.6)):
        lhs = (cb - d2) * (db - d1)
        M = d2 * db + d1 * cb
        rhs = cb * db - M + d1 * d2
        assert abs(lhs - rhs) < 1e-9 * max(1.0, abs(lhs))
        assert d1 * d2 >= 0, "dropped cross term must be non-negative"


def test_dbar_is_a_discounted_sum_to_the_signal_date():
    """Dbar(T) = sum_{t_i <= T} delta_i exp(-r t_i), discounted to t0."""
    divs = [Dividend(date(2026, 9, 18), 1.80), Dividend(date(2026, 12, 18), 1.90)]
    r = 0.05
    for horizon in (date(2026, 10, 1), date(2026, 12, 31), date(2027, 6, 1)):
        expected = sum(
            d.amount * math.exp(-r * ((d.ex_date - SIGNAL).days / 365.0))
            for d in divs
            if SIGNAL < d.ex_date <= horizon
        )
        got = discounted_dividend_total(divs, SIGNAL, horizon, r)
        assert abs(got - expected) < 1e-12, (horizon, got, expected)


def test_dbar_is_monotone_nondecreasing_in_horizon():
    divs = [Dividend(date(2026, 9, 18), 1.8), Dividend(date(2026, 12, 18), 1.9)]
    vals = [
        discounted_dividend_total(divs, SIGNAL, SIGNAL + timedelta(days=n), 0.045)
        for n in (10, 30, 120, 200, 400)
    ]
    assert vals == sorted(vals), vals


# ==========================================================================
# 6. Tick quantisation
# ==========================================================================


def test_tick_bound_is_first_order_correct():
    """Relative error of a product is the sum of the per-factor relative errors.

    Compared against a brute-force worst case: perturb each factor by +/- tick/2
    in the direction that maximises the product's relative deviation.
    """
    for prices in ([0.60, 0.80, 1.20, 2.00], [5.0, 8.0, 12.0, 20.0], [300.0] * 4):
        quotes = [Quote(p - 0.01, p + 0.01) for p in prices]
        predicted = tick_error_bound(quotes, DEFAULT_TICK)
        half = DEFAULT_TICK / 2.0
        base = math.prod(prices)
        worst = max(
            abs(math.prod(p + s * half for p, s in zip(prices, signs)) - base) / base
            for signs in product((1, -1), repeat=len(prices))
        )
        assert predicted >= worst - 1e-9, (prices, predicted, worst)
        assert predicted <= worst * 1.05 + 1e-9, "bound is loose beyond 5%"


def test_tick_bound_dominates_on_cheap_legs():
    """On penny options the quantisation error swamps a 2% signal."""
    cheap = [Quote(0.06, 0.08)] * 4
    assert tick_error_bound(cheap) > 0.20
    rich = [Quote(299.0, 301.0)] * 4
    assert tick_error_bound(rich) < 0.0001


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

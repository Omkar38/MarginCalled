"""Tests for the theory gate.

Runs standalone (`python3 tests/test_theory_gate.py`) with no third-party
dependencies, and is also collectable by pytest if it is installed.

The central test is `test_break_even_horizon_matches_paper`, which asserts that the
Proposition 2.1(ii) implementation flips at the horizons stated in the paper:
"for delta_i / K = 0.003, the single-dividend break-even horizon is about 55
calendar days at r = 2% and 22 at r = 5%". Those follow from -ln(0.997)/r, giving
54.8 and 21.9 days. If this test fails, the implementation does not reproduce the
published result and is wrong.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.theory_gate import (  # noqa: E402
    Category,
    Contract,
    Dividend,
    Rectangle,
    classify,
    discounted_dividend_total,
    zero_premium_holds,
)

SIGNAL = date(2026, 8, 31)


def _rect(
    t1: date,
    t2: date,
    k1: float = 640.0,
    k2: float = 650.0,
    k1t: float = 642.0,
    k2t: float = 648.0,
    c_bid: float = 0.0,
    d_bid: float = 0.0,
    signal: date = SIGNAL,
) -> Rectangle:
    return Rectangle(
        signal_date=signal,
        A=Contract("A", k1, t1),
        B=Contract("B", k2, t2),
        C=Contract("C", k1t, t2, bid=c_bid),
        D=Contract("D", k2t, t1, bid=d_bid),
    )


# --------------------------------------------------------------------------
# Proposition 2.1(i): no distribution before the far maturity
# --------------------------------------------------------------------------


def test_no_dividends_is_no_distribution():
    rect = _rect(date(2026, 9, 11), date(2026, 9, 25))
    res = classify(rect, dividends=[], r=0.04)
    assert res.category is Category.NO_DISTRIBUTION
    assert res.category.is_european_equivalent
    assert res.is_tradable
    assert all(res.per_contract_zero_premium.values())


def test_dividend_after_T2_is_ignored():
    rect = _rect(date(2026, 9, 11), date(2026, 9, 18))
    divs = [Dividend(date(2026, 9, 25), 1.80)]  # after T2
    res = classify(rect, divs, r=0.04)
    assert res.category is Category.NO_DISTRIBUTION
    assert res.dividends_used == []


def test_dividend_on_or_before_signal_is_ignored():
    rect = _rect(date(2026, 9, 11), date(2026, 9, 25))
    divs = [Dividend(SIGNAL, 1.80)]  # not strictly after t0
    res = classify(rect, divs, r=0.04)
    assert res.category is Category.NO_DISTRIBUTION


# --------------------------------------------------------------------------
# Proposition 2.1(ii): the published break-even horizons
# --------------------------------------------------------------------------


def test_break_even_horizon_matches_paper():
    """delta/K = 0.003 -> ~55 days at r=2%, ~22 days at r=5% (paper, Section 2)."""
    strike = 100.0
    amount = 0.3  # delta / K = 0.003
    ex = date(2026, 9, 18)

    for rate, boundary in ((0.02, 55), (0.05, 22)):
        holds = zero_premium_holds(
            strike, ex + timedelta(days=boundary), [Dividend(ex, amount)], SIGNAL, rate
        )
        fails = zero_premium_holds(
            strike,
            ex + timedelta(days=boundary - 1),
            [Dividend(ex, amount)],
            SIGNAL,
            rate,
        )
        assert holds, f"r={rate}: expected condition to hold at {boundary} days"
        assert not fails, f"r={rate}: expected failure at {boundary - 1} days"


def test_condition_is_monotone_in_rate():
    """Higher rates make deferring the strike more valuable, so the condition eases."""
    strike, ex = 100.0, date(2026, 9, 18)
    divs = [Dividend(ex, 0.3)]
    expiry = ex + timedelta(days=30)
    assert not zero_premium_holds(strike, expiry, divs, SIGNAL, 0.02)
    assert zero_premium_holds(strike, expiry, divs, SIGNAL, 0.05)


def test_zero_rate_fails_for_any_spanning_contract():
    """At r ~ 0 the deferral benefit vanishes; the paper notes this for 2021."""
    strike, ex = 100.0, date(2026, 9, 18)
    divs = [Dividend(ex, 0.3)]
    for horizon in (10, 60, 365, 720):
        assert not zero_premium_holds(
            strike, ex + timedelta(days=horizon), divs, SIGNAL, 1e-9
        )


def test_all_four_legs_must_pass():
    """A rectangle is European-equivalent only if every contract has zero premium."""
    t1, t2 = date(2026, 9, 25), date(2026, 10, 16)
    ex = date(2026, 9, 18)
    divs = [Dividend(ex, 0.3)]
    # Very low strike on leg D: deferral benefit K(1-e^{-rT}) is small, so it fails
    # while the high-strike legs pass.
    rect = _rect(t1, t2, k1=600.0, k2=650.0, k1t=620.0, k2t=1.0)
    res = classify(rect, divs, r=0.04, violation_size=0.0)
    assert res.per_contract_zero_premium["D"] is False
    assert res.category is not Category.DIVIDEND_SPANNING


def test_dividend_spanning_when_all_legs_pass():
    # T1 must clear the ex-date by more than the break-even horizon: at r=5% a
    # $1.80 distribution against a ~$640 strike needs ~21 days of deferral, so a
    # near leg expiring 7 days after the ex-date would fail. See
    # test_near_leg_too_close_to_ex_date_fails.
    t1, t2 = date(2026, 10, 16), date(2026, 12, 18)
    divs = [Dividend(date(2026, 9, 18), 1.80)]
    rect = _rect(t1, t2)
    res = classify(rect, divs, r=0.05)
    assert res.category is Category.DIVIDEND_SPANNING
    assert res.category.is_european_equivalent
    assert res.is_tradable


def test_near_leg_too_close_to_ex_date_fails():
    """Design constraint for the live universe.

    A realistic SPY distribution (~$1.80 against a ~$640 strike) needs roughly 21
    calendar days between the ex-date and expiry at r=5% before Proposition 2.1(ii)
    can hold. A near leg expiring inside that window can never be certified
    zero-premium, so the scanner must not build rectangles whose T1 sits just past
    an ex-dividend date.
    """
    ex = date(2026, 9, 18)
    divs = [Dividend(ex, 1.80)]
    assert not zero_premium_holds(640.0, ex + timedelta(days=7), divs, SIGNAL, 0.05)
    assert not zero_premium_holds(640.0, ex + timedelta(days=20), divs, SIGNAL, 0.05)
    assert zero_premium_holds(640.0, ex + timedelta(days=21), divs, SIGNAL, 0.05)


# --------------------------------------------------------------------------
# Proposition 2.2: the dividend-bound certificate
# --------------------------------------------------------------------------


def test_dbar_is_discounted_sum():
    divs = [Dividend(date(2026, 9, 18), 1.80), Dividend(date(2026, 12, 18), 1.90)]
    got = discounted_dividend_total(divs, SIGNAL, date(2026, 12, 31), r=0.05)
    assert abs(got - 3.667408) < 1e-4, got
    near = discounted_dividend_total(divs, SIGNAL, date(2026, 10, 1), r=0.05)
    assert abs(near - 1.7955) < 1e-3, near


def test_certificate_holds_gives_dividend_bound():
    t1, t2 = date(2026, 9, 25), date(2026, 12, 18)
    divs = [Dividend(date(2026, 9, 18), 1.80)]
    rect = _rect(t1, t2, k2t=1.0, c_bid=2.0, d_bid=1.5)  # leg D fails Prop 2.1(ii)
    res = classify(rect, divs, r=0.02, violation_size=100.0)
    assert res.category is Category.DIVIDEND_BOUND
    assert res.bound_M is not None and res.violation_size > res.bound_M
    assert res.is_tradable
    assert not res.category.is_european_equivalent


def test_certificate_fails_gives_unresolved():
    t1, t2 = date(2026, 9, 25), date(2026, 12, 18)
    divs = [Dividend(date(2026, 9, 18), 1.80)]
    rect = _rect(t1, t2, k2t=1.0, c_bid=2.0, d_bid=1.5)
    res = classify(rect, divs, r=0.02, violation_size=1e-6)
    assert res.category is Category.UNRESOLVED
    assert not res.is_tradable


def test_unresolved_without_violation_size():
    t1, t2 = date(2026, 9, 25), date(2026, 12, 18)
    divs = [Dividend(date(2026, 9, 18), 1.80)]
    rect = _rect(t1, t2, k2t=1.0)
    res = classify(rect, divs, r=0.02)
    assert res.category is Category.UNRESOLVED
    assert any("not supplied" in reason for reason in res.reasons)


# --------------------------------------------------------------------------
# Provenance: amounts not announced at signal time must not be used
# --------------------------------------------------------------------------


def test_unannounced_dividend_is_discarded():
    """The paper excludes dividend fields from features because amounts can be
    finalised after the signal date. The gate must not silently use them."""
    rect = _rect(date(2026, 9, 25), date(2026, 12, 18))
    divs = [
        Dividend(date(2026, 9, 18), 1.80, announced_on=date(2026, 9, 15)),  # later
    ]
    res = classify(rect, divs, r=0.02)
    assert res.category is Category.NO_DISTRIBUTION
    assert res.dividends_used == []
    assert any("discarded" in reason for reason in res.reasons)


def test_announced_dividend_is_used():
    rect = _rect(date(2026, 10, 16), date(2026, 12, 18))
    divs = [Dividend(date(2026, 9, 18), 1.80, announced_on=date(2026, 8, 20))]
    res = classify(rect, divs, r=0.05)
    assert res.category is Category.DIVIDEND_SPANNING
    assert len(res.dividends_used) == 1


# --------------------------------------------------------------------------
# Audit record
# --------------------------------------------------------------------------


def test_record_is_json_serialisable():
    import json

    rect = _rect(date(2026, 9, 11), date(2026, 9, 25))
    res = classify(rect, [], r=0.04)
    text = json.dumps(res.to_record())
    assert "no_distribution" in text


def test_multi_dividend_forgone_sum():
    """With two ex-dates before expiry, the first test must include the second."""
    strike = 100.0
    ex1, ex2 = date(2026, 9, 18), date(2026, 12, 18)
    divs = [Dividend(ex1, 0.3), Dividend(ex2, 0.3)]
    expiry = date(2026, 12, 20)
    # Single-dividend view would look satisfiable at this horizon; the second
    # distribution is what pushes the first ex-date over its threshold.
    assert not zero_premium_holds(strike, expiry, divs, SIGNAL, 0.02)
    assert zero_premium_holds(strike, expiry, [Dividend(ex1, 0.3)], SIGNAL, 0.02)


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

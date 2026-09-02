"""Tests for live F* feature construction.

These pin the definitions that a wrong value would NOT surface downstream. A
flipped IV sign or a mid/quote mix-up does not raise anywhere - it moves a model
input off the distribution it was fitted on and quietly degrades every decision
after it. Each definition here was verified bit-exact against the study's own
354,974-row token dataset; see scripts/validate_features_against_study.py.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tp2agent.features import (  # noqa: E402
    F_STAR_48,
    F_STAR_LIVE,
    LIVE_UNAVAILABLE,
    STRATEGY_DEPENDENT,
    STUDY_EPS,
    FeatureError,
    build_features,
    feature_vector,
)
from tp2agent.rectangles import OptionQuote, Quote, RectangleCandidate  # noqa: E402

SIG = date(2026, 9, 2)


def _leg(sym, strike, bid, ask, iv, delta=0.5, theta=-0.1, vega=0.2):
    return OptionQuote(
        sym, strike, SIG, "C", Quote(bid, ask, 10.0, 10.0),
        {"delta": delta, "theta": theta, "vega": vega}, iv,
    )


def _cand():
    A = _leg("A", 640.0, 9.0, 10.0, 0.20, 0.55, -0.11, 0.21)
    B = _leg("B", 650.0, 5.0, 6.0, 0.24, 0.45, -0.12, 0.22)
    C = _leg("C", 641.0, 6.0, 7.0, 0.22, 0.50, -0.13, 0.23)
    D = _leg("D", 649.0, 8.0, 9.0, 0.26, 0.40, -0.14, 0.24)
    return RectangleCandidate(
        signal_date=SIG, T1=date(2026, 9, 18), T2=date(2026, 10, 16),
        K1=640.0, K2=650.0, K1_adj=641.0, K2_adj=649.0,
        A=A, B=B, C=C, D=D, F_T1=645.0, F_T2=647.0,
        lhs=A.quote.ask * B.quote.ask, rhs=C.quote.bid * D.quote.bid,
        violation_size=C.quote.bid * D.quote.bid - A.quote.ask * B.quote.ask,
        tick_bound=0.0, coverage_ratio=1.0,
    )


# --------------------------------------------------------------------------
# The feature list
# --------------------------------------------------------------------------


def test_f_star_is_48_and_live_is_46():
    assert len(F_STAR_48) == 48
    assert len(F_STAR_LIVE) == 46
    assert len(set(F_STAR_48)) == 48, "no duplicates"


def test_live_list_drops_exactly_the_unavailable_two():
    assert LIVE_UNAVAILABLE == {"lc_pressure_max_le0", "feature_D_open_interest"}
    assert set(F_STAR_48) - set(F_STAR_LIVE) == LIVE_UNAVAILABLE


def test_live_list_preserves_f_star_order():
    """Order is the contract between training and inference; it must not be
    re-sorted, only filtered."""
    assert list(F_STAR_LIVE) == [f for f in F_STAR_48 if f not in LIVE_UNAVAILABLE]


# --------------------------------------------------------------------------
# TP2 geometry: the mid / crossable-quote split
# --------------------------------------------------------------------------


def test_severity_uses_mid_prices_not_crossable_quotes():
    """feature_severity is C_mid*D_mid - A_mid*B_mid, verified to 0.00e+00
    against the study dataset. Computing it from A_ask*B_ask and C_bid*D_bid -
    the conservative_bidask_* convention - is a different quantity."""
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    mid_lhs = c.A.quote.mid * c.B.quote.mid
    mid_rhs = c.C.quote.mid * c.D.quote.mid
    assert abs(f["feature_severity"] - (mid_rhs - mid_lhs)) < 1e-12
    assert abs(f["feature_rhs"] - mid_rhs) < 1e-12
    # and is NOT the conservative quantity
    assert abs(f["feature_severity"] - (c.rhs - c.lhs)) > 1e-9


def test_normalized_severity_divides_by_the_sum_of_mid_products():
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    mid_lhs = c.A.quote.mid * c.B.quote.mid
    mid_rhs = c.C.quote.mid * c.D.quote.mid
    assert abs(
        f["feature_normalized_severity"] - (mid_rhs - mid_lhs) / (mid_lhs + mid_rhs)
    ) < 1e-12


def test_executable_ratio_uses_crossable_quotes_and_the_study_epsilon():
    """This one feature is deliberately NOT mid-based: it exists to say whether
    a mid-priced signal survives at prices a trade must actually cross."""
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    ba_lhs = c.A.quote.ask * c.B.quote.ask
    ba_rhs = c.C.quote.bid * c.D.quote.bid
    assert abs(
        f["feature_bidask_executable_ratio"] - ba_rhs / (ba_lhs + STUDY_EPS)
    ) < 1e-15
    assert STUDY_EPS == 1e-9


def test_model_features_ignore_the_candidates_own_severity():
    """RectangleCandidate.normalized_severity is the conservative detection-side
    number. Reusing it as the model feature would feed the wrong quantity."""
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    assert abs(f["feature_normalized_severity"] - c.normalized_severity) > 1e-9


# --------------------------------------------------------------------------
# IV block signs
# --------------------------------------------------------------------------


def test_iv_signs_match_the_study():
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    assert abs(f["feature_iv_slope_T1"] - (c.D.iv - c.A.iv)) < 1e-12
    assert abs(f["feature_iv_slope_T2"] - (c.B.iv - c.C.iv)) < 1e-12
    assert abs(f["feature_iv_term_K2"] - (c.B.iv - c.D.iv)) < 1e-12
    assert abs(f["feature_avg_iv"] - (c.A.iv + c.B.iv + c.C.iv + c.D.iv) / 4) < 1e-12


def test_iv_slope_T2_is_B_minus_C_not_the_reverse():
    """A sign flip here is silent: the value stays plausible and in range, so
    the fixture is built with B_iv != C_iv to make the direction observable."""
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    assert c.B.iv > c.C.iv, "fixture must make the two IVs distinguishable"
    assert f["feature_iv_slope_T2"] > 0, "B_iv - C_iv is positive for this fixture"
    assert abs(f["feature_iv_slope_T2"] - 0.02) < 1e-12
    assert abs(f["feature_iv_slope_T2"] + 0.02) > 1e-9, "the reversed sign"


# --------------------------------------------------------------------------
# Strategy dependence
# --------------------------------------------------------------------------


def test_only_two_features_differ_between_denominations():
    """F* has no is_t1/is_k2/strategy_id, so the denomination reaches the model
    through exactly these two numbers and nothing else."""
    c = _cand()
    t1 = build_features(c, "T1", 645.0, SIG)
    k2 = build_features(c, "K2", 645.0, SIG)
    differing = {k for k in t1 if abs(t1[k] - k2[k]) > 1e-15}
    assert differing == STRATEGY_DEPENDENT, f"unexpected difference: {differing}"


def test_t1_reads_legs_A_and_D():
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    assert abs(f["strategy_selected_mid_sum"] - (c.A.quote.mid + c.D.quote.mid)) < 1e-12


def test_k2_reads_legs_B_and_D():
    c = _cand()
    f = build_features(c, "K2", 645.0, SIG)
    assert abs(f["strategy_selected_mid_sum"] - (c.B.quote.mid + c.D.quote.mid)) < 1e-12


def test_unknown_strategy_is_refused():
    try:
        build_features(_cand(), "T2", 645.0, SIG)
    except FeatureError as exc:
        assert "T1" in str(exc) and "K2" in str(exc)
        return
    raise AssertionError("an unsupported denomination must be refused")


# --------------------------------------------------------------------------
# Surface location
# --------------------------------------------------------------------------


def test_dte_and_gaps_are_calendar_days():
    c = _cand()
    f = build_features(c, "T1", 645.0, SIG)
    assert f["feature_T1_DTE"] == 16.0
    assert f["feature_T2_DTE"] == 44.0
    assert f["feature_term_gap"] == 28.0
    assert f["feature_strike_gap"] == 10.0
    assert f["signal_month"] == 9.0


# --------------------------------------------------------------------------
# The vector
# --------------------------------------------------------------------------


def test_vector_is_46_long_and_in_order():
    v = feature_vector(_cand(), "T1", 645.0, SIG)
    assert len(v) == 46
    row = build_features(_cand(), "T1", 645.0, SIG)
    assert v == [row[n] for n in F_STAR_LIVE]


def test_vector_refuses_a_missing_greek():
    """A NaN must stop the trade, not become a plausible-looking input."""
    c = _cand()
    c.D.greeks.pop("theta")
    try:
        feature_vector(c, "T1", 645.0, SIG)
    except FeatureError as exc:
        assert "non-finite" in str(exc) and "feature_D_theta" in str(exc)
        return
    raise AssertionError("a missing greek must refuse the vector")


def test_vector_refuses_a_missing_iv():
    from dataclasses import replace

    c = _cand()
    c = replace(c, B=replace(c.B, iv=None))
    try:
        feature_vector(c, "T1", 645.0, SIG)
    except FeatureError:
        return
    raise AssertionError("a missing IV must refuse the vector")


def test_nonpositive_underlying_is_refused():
    try:
        build_features(_cand(), "T1", 0.0, SIG)
    except FeatureError as exc:
        assert "underlying_price" in str(exc)
        return
    raise AssertionError("a non-positive spot must be refused")


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

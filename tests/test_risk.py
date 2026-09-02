"""Tests for the deterministic risk gates."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tp2agent.position import PositionConfig, Structure, build_position  # noqa: E402
from tp2agent.risk import (  # noqa: E402
    AccountState,
    RejectCode,
    RiskDecision,
    RiskLimits,
    evaluate,
    revalidate,
    should_flatten,
)
from tp2agent.theory_gate import Category  # noqa: E402

from test_position import _realistic_candidate  # noqa: E402

NOW = datetime(2026, 9, 1, 15, 50)  # Tue 1 Sep, inside the entry window


def _spec():
    return build_position(
        _realistic_candidate(), PositionConfig(structure=Structure.FOUR_LEG)
    )


def _account(**kw):
    base = dict(
        equity=100_000.0,
        starting_equity=100_000.0,
        day_realized_pnl=0.0,
        open_position_count=0,
        open_leg_symbols=frozenset(),
        committed_max_loss=0.0,
        buying_power=100_000.0,
    )
    base.update(kw)
    return AccountState(**base)


def _limits(**kw):
    return replace(RiskLimits(), **kw) if kw else RiskLimits()


def _evaluate(spec=None, account=None, limits=None, now=NOW, age=1.0, category=None):
    return evaluate(
        spec or _spec(),
        category or Category.DIVIDEND_SPANNING,
        account or _account(),
        limits or _limits(),
        now,
        age,
    )


def _codes(decision: RiskDecision) -> set[RejectCode]:
    return {code for code, _ in decision.rejections}


def _violating(size: float = 1.0, tick_bound: float = 0.0):
    """A candidate carrying a genuine positive violation.

    `_realistic_candidate()` is drawn from a clean Black-Scholes chain, so its
    violation_size is negative by construction (TP2 holds). Re-validation tests
    need a violating baseline to decay away from.
    """
    return replace(_realistic_candidate(), violation_size=size, tick_bound=tick_bound)


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def test_clean_trade_is_approved():
    # A four-leg spread on $5-wide strikes can exceed a 0.25% cap, so this test
    # uses a cap wide enough to isolate the non-sizing gates.
    d = _evaluate(limits=_limits(max_loss_per_trade_pct=0.02, max_aggregate_loss_pct=0.05))
    assert d.approved, d.rejections
    assert not d.rejections
    assert len(d.checks_passed) >= 6


def test_every_failure_is_collected_not_just_the_first():
    """One rejection must never mask another; the audit needs all of them."""
    d = _evaluate(
        account=_account(kill_switch_engaged=True, open_position_count=99),
        limits=_limits(max_loss_per_trade_pct=1e-9),
    )
    codes = _codes(d)
    assert RejectCode.KILL_SWITCH in codes
    assert RejectCode.TOO_MANY_POSITIONS in codes
    assert RejectCode.MAX_LOSS_PER_TRADE in codes
    assert len(codes) >= 3


# --------------------------------------------------------------------------
# Session halts
# --------------------------------------------------------------------------


def test_kill_switch_blocks():
    d = _evaluate(account=_account(kill_switch_engaged=True))
    assert not d.approved
    assert RejectCode.KILL_SWITCH in _codes(d)


def test_reconciliation_failure_blocks():
    d = _evaluate(account=_account(reconciliation_ok=False))
    assert RejectCode.RECONCILIATION_ERROR in _codes(d)


def test_daily_stop_blocks():
    d = _evaluate(account=_account(day_realized_pnl=-800.0))  # 0.8% > 0.75%
    assert RejectCode.DAILY_STOP in _codes(d)


def test_daily_stop_not_triggered_by_profit():
    d = _evaluate(
        account=_account(day_realized_pnl=+5000.0),
        limits=_limits(max_loss_per_trade_pct=0.02, max_aggregate_loss_pct=0.05),
    )
    assert RejectCode.DAILY_STOP not in _codes(d)


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------


def test_after_daily_cutoff_blocks():
    d = _evaluate(now=datetime(2026, 9, 1, 15, 58))
    assert RejectCode.AFTER_DAILY_CUTOFF in _codes(d)


def test_after_entry_deadline_blocks():
    d = _evaluate(now=datetime(2026, 9, 4, 10, 0))  # Fri 4 Sep
    assert RejectCode.AFTER_ENTRY_DEADLINE in _codes(d)


def test_deadline_takes_precedence_over_cutoff():
    """Past the deadline the reason should be the deadline, not the time of day."""
    d = _evaluate(now=datetime(2026, 9, 4, 15, 58))
    codes = _codes(d)
    assert RejectCode.AFTER_ENTRY_DEADLINE in codes
    assert RejectCode.AFTER_DAILY_CUTOFF not in codes


# --------------------------------------------------------------------------
# Theory
# --------------------------------------------------------------------------


def test_unresolved_category_blocks():
    d = _evaluate(category=Category.UNRESOLVED)
    assert RejectCode.THEORY_UNRESOLVED in _codes(d)


def test_all_certified_categories_permitted():
    wide = _limits(max_loss_per_trade_pct=0.02, max_aggregate_loss_pct=0.05)
    for cat in (
        Category.NO_DISTRIBUTION,
        Category.DIVIDEND_SPANNING,
        Category.DIVIDEND_BOUND,
    ):
        d = _evaluate(category=cat, limits=wide)
        assert RejectCode.THEORY_UNRESOLVED not in _codes(d), cat


# --------------------------------------------------------------------------
# Staleness and re-validation  (the gate the user asked for)
# --------------------------------------------------------------------------


def test_stale_quotes_block():
    d = _evaluate(age=120.0)
    assert RejectCode.QUOTES_STALE in _codes(d)


def test_violation_gone_blocks():
    """The market corrected between detection and send."""
    d = RiskDecision()
    revalidate(_violating(1.0), None, _limits(), d)
    assert RejectCode.VIOLATION_GONE in _codes(d)


def test_violation_decayed_blocks():
    detected = _violating(1.0)
    fresh = replace(detected, violation_size=0.2)
    d = RiskDecision()
    revalidate(detected, fresh, _limits(min_violation_retained=0.5), d)
    assert RejectCode.VIOLATION_DECAYED in _codes(d)
    assert d.violation_retained is not None
    assert abs(d.violation_retained - 0.2) < 1e-9


def test_violation_retained_passes():
    detected = _violating(1.0)
    fresh = replace(detected, violation_size=0.9, tick_bound=0.0)
    d = RiskDecision()
    revalidate(detected, fresh, _limits(min_violation_retained=0.5), d)
    assert not d.rejections, d.rejections
    assert any("revalidated" in c for c in d.checks_passed)


def test_fresh_violation_must_still_clear_tick_bound():
    detected = _violating(1.0)
    # Retains 90% of size, but the tick bound now swallows it.
    fresh = replace(detected, violation_size=0.9, tick_bound=0.99)
    d = RiskDecision()
    revalidate(detected, fresh, _limits(), d)
    assert RejectCode.VIOLATION_DECAYED in _codes(d)


def test_zero_or_negative_fresh_violation_blocks():
    detected = _violating(1.0)
    fresh = replace(detected, violation_size=-0.01)
    d = RiskDecision()
    revalidate(detected, fresh, _limits(), d)
    assert RejectCode.VIOLATION_GONE in _codes(d)


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------


def test_per_trade_cap_blocks():
    d = _evaluate(limits=_limits(max_loss_per_trade_pct=1e-9))
    assert RejectCode.MAX_LOSS_PER_TRADE in _codes(d)


def test_aggregate_cap_blocks():
    d = _evaluate(
        account=_account(committed_max_loss=999.0),
        limits=_limits(max_loss_per_trade_pct=0.02, max_aggregate_loss_pct=0.01),
    )
    assert RejectCode.MAX_AGGREGATE_LOSS in _codes(d)


def test_sizing_uses_max_loss_including_commissions():
    """When costs are charged, the sizing cap must count them.

    The default is zero (Alpaca is commission-free and paper simulates no fees),
    so this charges the source study's IBKR rate explicitly to verify the cap
    responds to costs rather than ignoring them.
    """
    from tp2agent.position import COMMISSION_IBKR_LITE

    cfg = PositionConfig(
        structure=Structure.FOUR_LEG,
        commission_per_contract_side=COMMISSION_IBKR_LITE,
    )
    spec = build_position(_realistic_candidate(), cfg)
    assert spec.max_loss_with_commissions > spec.max_loss

    # A cap set between the two figures must reject on the cost-inclusive number.
    cap_pct = (spec.max_loss + 0.01) / 100_000.0
    d = _evaluate(
        spec=spec,
        limits=_limits(max_loss_per_trade_pct=cap_pct, max_aggregate_loss_pct=0.05),
    )
    assert RejectCode.MAX_LOSS_PER_TRADE in _codes(d), "commissions must count"


def test_zero_cost_default_does_not_inflate_sizing():
    spec = _spec()
    assert spec.commissions_round_trip == 0.0
    assert spec.max_loss_with_commissions == spec.max_loss


def test_insufficient_buying_power_blocks():
    d = _evaluate(
        account=_account(buying_power=1.0),
        limits=_limits(max_loss_per_trade_pct=0.5, max_aggregate_loss_pct=0.5),
    )
    assert RejectCode.INSUFFICIENT_BUYING_POWER in _codes(d)


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------


def test_position_count_cap_blocks():
    d = _evaluate(account=_account(open_position_count=5), limits=_limits(max_open_positions=5))
    assert RejectCode.TOO_MANY_POSITIONS in _codes(d)


def test_duplicate_leg_blocks():
    spec = _spec()
    held = frozenset({spec.legs[0].symbol})
    d = _evaluate(spec=spec, account=_account(open_leg_symbols=held))
    assert RejectCode.DUPLICATE_LEG in _codes(d)
    msg = dict((c.value, m) for c, m in d.rejections)[RejectCode.DUPLICATE_LEG.value]
    assert "wash-trade" in msg


def test_disjoint_legs_allowed():
    d = _evaluate(
        account=_account(open_leg_symbols=frozenset({"SPY_SOMETHING_ELSE"})),
        limits=_limits(max_loss_per_trade_pct=0.02, max_aggregate_loss_pct=0.05),
    )
    assert RejectCode.DUPLICATE_LEG not in _codes(d)


# --------------------------------------------------------------------------
# Flatten
# --------------------------------------------------------------------------


def test_flatten_is_independent_of_entry_gates():
    """Flatten must work when every entry gate is failing."""
    flatten_at = datetime(2026, 9, 4, 9, 45)
    assert should_flatten(datetime(2026, 9, 4, 9, 45), flatten_at)
    assert should_flatten(datetime(2026, 9, 4, 10, 30), flatten_at)
    assert not should_flatten(datetime(2026, 9, 4, 9, 30), flatten_at)


def test_record_is_json_serialisable():
    import json

    d = _evaluate(account=_account(kill_switch_engaged=True))
    text = json.dumps(d.to_record())
    assert "kill_switch" in text


def test_revalidation_against_the_same_candidate_is_vacuous():
    """A guard against the bug this gate actually had.

    run_scanner passed the detected candidate as `fresh`, so revalidate compared
    the rectangle with itself: violation_retained was exactly 1.0 on every record
    and VIOLATION_GONE / VIOLATION_DECAYED could never fire. Once real
    re-pricing was wired in, 209 candidates in a single SPX scan failed those
    two gates - every one of which would otherwise have been sent.
    """
    # _realistic_candidate is not itself a violation (its violation_size is
    # negative), so give it a positive one - the gate only has meaning on a
    # rectangle that actually violates.
    cand = replace(_realistic_candidate(), violation_size=1.25)
    d = RiskDecision()
    revalidate(cand, cand, RiskLimits(), d)
    assert d.violation_retained == 1.0
    assert not d.rejections, "self-comparison can never reject - which is the bug"


def test_revalidation_catches_a_decayed_violation():
    cand = replace(_realistic_candidate(), violation_size=1.25)
    weaker = replace(cand, violation_size=cand.violation_size * 0.10)
    d = RiskDecision()
    revalidate(cand, weaker, RiskLimits(min_violation_retained=0.50), d)
    assert any(c is RejectCode.VIOLATION_DECAYED for c, _ in d.rejections)


def test_revalidation_catches_a_vanished_violation():
    d = RiskDecision()
    revalidate(_realistic_candidate(), None, RiskLimits(), d)
    assert any(c is RejectCode.VIOLATION_GONE for c, _ in d.rejections)


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

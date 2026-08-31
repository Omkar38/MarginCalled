"""Deterministic risk gates.

Every order passes through here, and nothing else may authorise a send. The model,
the narrator, and any future component can propose; only this module approves. All
gates are pure functions of explicit state, so a decision can be replayed exactly
from the audit log.

Gate order matters only for readability: every gate runs and every failure is
collected, so one rejection never masks another. That makes the audit record show
all the reasons a trade was refused rather than only the first.

The gates fall into four groups:

  Staleness and re-validation
      The window between detection and send is where a TP2 violation most often
      evaporates. Quotes are re-fetched immediately before the order is built and
      the violation is re-tested on the fresh quotes; a candidate that no longer
      violates, or has decayed materially, is dropped.

  Sizing
      Per-trade and aggregate caps on *defined* maximum loss, computed from the
      integer quantities actually being sent (see position.py), never from the
      theoretical weights.

  Portfolio and session
      Position count, duplicate leg exposure, daily loss stop, kill switch.

  Calendar
      No entry after the daily cutoff, and no entry at all after the contest
      entry deadline. The hard flatten is a separate operation, not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum

from .position import PositionSpec
from .rectangles import RectangleCandidate
from .theory_gate import Category

__all__ = [
    "RejectCode",
    "RiskLimits",
    "AccountState",
    "RiskDecision",
    "revalidate",
    "evaluate",
]


class RejectCode(str, Enum):
    """Stable reason codes. These appear in the audit log and the narration."""

    KILL_SWITCH = "kill_switch"
    NOT_EXECUTABLE = "position_not_executable"
    NOT_COVERED = "position_not_covered"
    THEORY_UNRESOLVED = "theory_unresolved"
    QUOTES_STALE = "quotes_stale"
    VIOLATION_GONE = "violation_gone"
    VIOLATION_DECAYED = "violation_decayed"
    MAX_LOSS_PER_TRADE = "max_loss_per_trade"
    MAX_AGGREGATE_LOSS = "max_aggregate_loss"
    DAILY_STOP = "daily_stop"
    TOO_MANY_POSITIONS = "too_many_positions"
    DUPLICATE_LEG = "duplicate_leg_exposure"
    AFTER_DAILY_CUTOFF = "after_daily_cutoff"
    AFTER_ENTRY_DEADLINE = "after_entry_deadline"
    RECONCILIATION_ERROR = "reconciliation_error"
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"


@dataclass(frozen=True)
class RiskLimits:
    """Starting engineering limits. Not optimised trading parameters."""

    # Sizing, as fractions of account equity
    max_loss_per_trade_pct: float = 0.0025  # 0.25%
    max_aggregate_loss_pct: float = 0.01  # 1%
    daily_stop_pct: float = 0.0075  # 0.75%

    # Portfolio
    max_open_positions: int = 5

    # Staleness and re-validation
    max_quote_age_seconds: float = 30.0
    min_violation_retained: float = 0.50  # fresh V must be >= 50% of detected V

    # Calendar
    daily_entry_cutoff: time = time(15, 55)
    entry_deadline: date = date(2026, 9, 3)  # no new entries after Thu 3 Sep

    # Which theory categories may trade at all
    tradable_categories: tuple[Category, ...] = (
        Category.EUROPEAN_NATIVE,
        Category.NO_DISTRIBUTION,
        Category.DIVIDEND_SPANNING,
        Category.DIVIDEND_BOUND,
    )


@dataclass
class AccountState:
    """Everything the gates need to know about the account, made explicit."""

    equity: float
    starting_equity: float
    day_realized_pnl: float = 0.0
    open_position_count: int = 0
    open_leg_symbols: frozenset[str] = frozenset()
    committed_max_loss: float = 0.0
    buying_power: float = 0.0
    kill_switch_engaged: bool = False
    reconciliation_ok: bool = True

    @property
    def day_loss_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return -min(self.day_realized_pnl, 0.0) / self.starting_equity


@dataclass
class RiskDecision:
    approved: bool = False
    rejections: list[tuple[RejectCode, str]] = field(default_factory=list)
    checks_passed: list[str] = field(default_factory=list)
    fresh_violation_size: float | None = None
    violation_retained: float | None = None

    def reject(self, code: RejectCode, message: str) -> None:
        self.rejections.append((code, message))

    def to_record(self) -> dict:
        return {
            "approved": self.approved,
            "rejections": [
                {"code": code.value, "message": msg} for code, msg in self.rejections
            ],
            "checks_passed": list(self.checks_passed),
            "fresh_violation_size": self.fresh_violation_size,
            "violation_retained": self.violation_retained,
        }


def revalidate(
    detected: RectangleCandidate,
    fresh: RectangleCandidate | None,
    limits: RiskLimits,
    decision: RiskDecision,
) -> None:
    """Re-test the violation on quotes fetched immediately before sending.

    `fresh` is the same rectangle re-priced from a new snapshot, or None if it no
    longer survives detection at all. This is the gate that catches the case where
    the market corrects between the scan and the order.
    """
    if fresh is None:
        decision.reject(
            RejectCode.VIOLATION_GONE,
            "rectangle no longer violates on fresh quotes; the market corrected "
            "between detection and send",
        )
        return

    decision.fresh_violation_size = fresh.violation_size

    if fresh.violation_size <= 0:
        decision.reject(
            RejectCode.VIOLATION_GONE,
            f"fresh violation size {fresh.violation_size:.6f} <= 0",
        )
        return

    retained = (
        fresh.violation_size / detected.violation_size
        if detected.violation_size > 0
        else 0.0
    )
    decision.violation_retained = retained

    if retained < limits.min_violation_retained:
        decision.reject(
            RejectCode.VIOLATION_DECAYED,
            f"violation decayed to {retained:.1%} of detected size "
            f"({fresh.violation_size:.4f} vs {detected.violation_size:.4f}); "
            f"minimum is {limits.min_violation_retained:.0%}",
        )
        return

    # The fresh quotes must still clear the tick-quantisation bound.
    required = fresh.rhs * fresh.tick_bound
    if fresh.violation_size <= required:
        decision.reject(
            RejectCode.VIOLATION_DECAYED,
            f"fresh violation {fresh.violation_size:.6f} no longer clears the tick "
            f"bound {required:.6f}",
        )
        return

    decision.checks_passed.append(
        f"revalidated: {retained:.1%} of detected violation retained"
    )


def evaluate(
    spec: PositionSpec,
    category: Category,
    account: AccountState,
    limits: RiskLimits,
    now: datetime,
    quote_age_seconds: float,
    detected: RectangleCandidate | None = None,
    fresh: RectangleCandidate | None = None,
) -> RiskDecision:
    """Run every gate. Approval requires all of them to pass.

    Returns a decision carrying every failure, not just the first, so the audit
    record and the narration can state all the reasons a trade was refused.
    """
    d = RiskDecision()

    # --- Session-level halts ------------------------------------------------
    if account.kill_switch_engaged:
        d.reject(RejectCode.KILL_SWITCH, "kill switch engaged; no orders permitted")
    else:
        d.checks_passed.append("kill switch clear")

    if not account.reconciliation_ok:
        d.reject(
            RejectCode.RECONCILIATION_ERROR,
            "position/order reconciliation failed; refusing to trade on unknown state",
        )

    if account.day_loss_pct >= limits.daily_stop_pct:
        d.reject(
            RejectCode.DAILY_STOP,
            f"daily loss {account.day_loss_pct:.2%} has reached the "
            f"{limits.daily_stop_pct:.2%} stop",
        )
    else:
        d.checks_passed.append(f"daily loss {account.day_loss_pct:.2%} within stop")

    # --- Calendar -----------------------------------------------------------
    if now.date() > limits.entry_deadline:
        d.reject(
            RejectCode.AFTER_ENTRY_DEADLINE,
            f"{now.date().isoformat()} is past the entry deadline "
            f"{limits.entry_deadline.isoformat()}; exits only",
        )
    elif now.time() > limits.daily_entry_cutoff:
        d.reject(
            RejectCode.AFTER_DAILY_CUTOFF,
            f"{now.time().strftime('%H:%M')} is past the daily entry cutoff "
            f"{limits.daily_entry_cutoff.strftime('%H:%M')}",
        )
    else:
        d.checks_passed.append("within the entry window")

    # --- Theory -------------------------------------------------------------
    if category not in limits.tradable_categories:
        d.reject(
            RejectCode.THEORY_UNRESOLVED,
            f"theory category '{category.value}' is not tradable; the "
            f"early-exercise premium cannot be ruled out or bounded",
        )
    else:
        d.checks_passed.append(f"theory category '{category.value}' permitted")

    # --- Position validity --------------------------------------------------
    if not spec.is_executable:
        d.reject(
            RejectCode.NOT_EXECUTABLE,
            spec.rejected_reason or "position is not executable",
        )
    if not spec.is_covered:
        d.reject(RejectCode.NOT_COVERED, "position has uncovered short legs")

    # --- Staleness and re-validation ---------------------------------------
    if quote_age_seconds > limits.max_quote_age_seconds:
        d.reject(
            RejectCode.QUOTES_STALE,
            f"quotes are {quote_age_seconds:.1f}s old; maximum is "
            f"{limits.max_quote_age_seconds:.0f}s",
        )
    else:
        d.checks_passed.append(f"quotes {quote_age_seconds:.1f}s old")

    if detected is not None:
        revalidate(detected, fresh, limits, d)

    # --- Sizing -------------------------------------------------------------
    per_trade_cap = limits.max_loss_per_trade_pct * account.equity
    total_loss = spec.max_loss_with_commissions
    if total_loss > per_trade_cap:
        d.reject(
            RejectCode.MAX_LOSS_PER_TRADE,
            f"max loss ${total_loss:,.2f} exceeds the per-trade cap "
            f"${per_trade_cap:,.2f} ({limits.max_loss_per_trade_pct:.2%} of equity)",
        )
    else:
        d.checks_passed.append(
            f"max loss ${total_loss:,.2f} within per-trade cap ${per_trade_cap:,.2f}"
        )

    aggregate_cap = limits.max_aggregate_loss_pct * account.equity
    projected = account.committed_max_loss + total_loss
    if projected > aggregate_cap:
        d.reject(
            RejectCode.MAX_AGGREGATE_LOSS,
            f"projected aggregate max loss ${projected:,.2f} exceeds "
            f"${aggregate_cap:,.2f} ({limits.max_aggregate_loss_pct:.2%} of equity)",
        )
    else:
        d.checks_passed.append(f"aggregate max loss ${projected:,.2f} within cap")

    if account.buying_power > 0 and total_loss > account.buying_power:
        d.reject(
            RejectCode.INSUFFICIENT_BUYING_POWER,
            f"max loss ${total_loss:,.2f} exceeds buying power "
            f"${account.buying_power:,.2f}",
        )

    # --- Portfolio ----------------------------------------------------------
    if account.open_position_count >= limits.max_open_positions:
        d.reject(
            RejectCode.TOO_MANY_POSITIONS,
            f"{account.open_position_count} open positions; maximum is "
            f"{limits.max_open_positions}",
        )
    else:
        d.checks_passed.append(
            f"{account.open_position_count}/{limits.max_open_positions} positions open"
        )

    overlap = {leg.symbol for leg in spec.legs} & account.open_leg_symbols
    if overlap:
        d.reject(
            RejectCode.DUPLICATE_LEG,
            f"legs already held: {', '.join(sorted(overlap))}; stacking the same "
            f"contract concentrates exposure and can trip wash-trade protection",
        )
    else:
        d.checks_passed.append("no duplicate leg exposure")

    d.approved = not d.rejections
    return d


def should_flatten(now: datetime, flatten_after: datetime) -> bool:
    """Whether the hard flatten has been reached.

    Deliberately separate from `evaluate`: flattening is an instruction to exit,
    not a permission to enter, and must work even when every entry gate is failing.
    """
    return now >= flatten_after


def next_entry_window(now: datetime, cutoff: time, interval_minutes: int = 5) -> datetime:
    """Next scan time, clamped to the daily entry cutoff."""
    nxt = now + timedelta(minutes=interval_minutes)
    cutoff_dt = datetime.combine(now.date(), cutoff)
    return min(nxt, cutoff_dt) if now <= cutoff_dt else nxt

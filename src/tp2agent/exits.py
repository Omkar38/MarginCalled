"""Exit management: close a position when its violation reverts.

Entry is only half the strategy. A TP2 trade is a bet that a violation
corrects, so the exit is where the thesis is actually settled - and until this
existed the agent could open positions and never close them.

Three triggers, checked in order of authority:

  DEADLINE    the contest cutoff, or the near leg approaching expiry. Overrides
              everything: an open position at the deadline is not a position,
              it is an unresolved bet.
  REVERTED    the tracked episode has reverted - the determinant no longer
              violates. This is the thesis resolving as intended.
  TIME_STOP   the position has been held past a maximum without reverting. A
              violation that has not corrected in a long time is not correcting.

Closing inverts the entry: the leg bought to open is sold to close, the leg
sold to open is bought to close, as one multi-leg order. Legging out separately
would leave a naked short between the two fills, which is exactly the exposure
the covered structure exists to avoid.

Exit orders are priced with the same conservative shading as entries but in the
opposite direction - we demand terms better than the indicative quote implies -
except under DEADLINE, where getting flat matters more than the price and the
shade is relaxed. That is a deliberate asymmetry: a bad fill is recoverable, an
open position past the cutoff is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

from .position import Side

__all__ = [
    "ExitReason",
    "ExitPolicy",
    "OpenPosition",
    "ExitDecision",
    "should_exit",
    "build_close_legs",
]


class ExitReason(str, Enum):
    REVERTED = "reverted"
    TIME_STOP = "time_stop"
    DEADLINE = "deadline"
    EXPIRY_NEAR = "expiry_near"
    HOLD = "hold"


@dataclass(frozen=True)
class ExitPolicy:
    """When to close, and how hard to insist on a price."""

    max_hold_minutes: int = 120
    # Flatten everything by this moment regardless of state.
    flatten_after: datetime = datetime(2026, 9, 4, 9, 45)
    # Close if the near leg expires within this many days: an option about to
    # expire stops behaving like the instrument the thesis was written about.
    close_within_days_of_expiry: int = 2
    # Shading on exits, in package spreads. Relaxed to zero on a deadline exit,
    # where being flat matters more than the fill.
    shade_spreads: float = 0.5
    deadline_shade_spreads: float = 0.0


@dataclass
class OpenPosition:
    """A filled position and the episode that justified it."""

    episode_id: str
    underlying: str
    denomination: str
    order_id: str
    opened_at: datetime
    long_symbol: str
    short_symbol: str
    long_expiry: date
    short_expiry: date
    entry_long_price: float = 0.0
    entry_short_price: float = 0.0
    qty: int = 1
    closed_at: datetime | None = None
    close_order_id: str | None = None
    close_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def near_expiry(self) -> date:
        return min(self.long_expiry, self.short_expiry)

    def held_minutes(self, now: datetime) -> float:
        return (now - self.opened_at).total_seconds() / 60.0

    def to_record(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "underlying": self.underlying,
            "denomination": self.denomination,
            "order_id": self.order_id,
            "opened_at": self.opened_at.isoformat(timespec="seconds"),
            "long_symbol": self.long_symbol,
            "short_symbol": self.short_symbol,
            "long_expiry": self.long_expiry.isoformat(),
            "short_expiry": self.short_expiry.isoformat(),
            "entry_long_price": self.entry_long_price,
            "entry_short_price": self.entry_short_price,
            "qty": self.qty,
            "closed_at": self.closed_at.isoformat(timespec="seconds")
            if self.closed_at
            else None,
            "close_order_id": self.close_order_id,
            "close_reason": self.close_reason,
            "notes": list(self.notes),
        }


@dataclass
class ExitDecision:
    reason: ExitReason
    should_close: bool
    detail: str = ""
    urgent: bool = False  # deadline exits relax the price shading

    def to_record(self) -> dict:
        return {
            "reason": self.reason.value,
            "should_close": self.should_close,
            "detail": self.detail,
            "urgent": self.urgent,
        }


def should_exit(
    position: OpenPosition,
    episode_status: str | None,
    now: datetime,
    policy: ExitPolicy | None = None,
) -> ExitDecision:
    """Decide whether to close, and why.

    `episode_status` is the tracker's view of the rectangle - "reverted" once
    the determinant no longer violates. None means the episode is untracked,
    which is treated as a reason to hold rather than to close: an unknown state
    is not evidence the thesis resolved.
    """
    policy = policy or ExitPolicy()

    # 1. Deadline. Nothing outranks being flat.
    if now >= policy.flatten_after:
        return ExitDecision(
            ExitReason.DEADLINE,
            True,
            f"{now:%Y-%m-%d %H:%M} is at or past the flatten time "
            f"{policy.flatten_after:%Y-%m-%d %H:%M}",
            urgent=True,
        )

    # 2. Approaching expiry on the near leg.
    days_left = (position.near_expiry - now.date()).days
    if days_left <= policy.close_within_days_of_expiry:
        return ExitDecision(
            ExitReason.EXPIRY_NEAR,
            True,
            f"near leg expires {position.near_expiry.isoformat()} in {days_left} "
            f"day(s); within the {policy.close_within_days_of_expiry}-day limit",
            urgent=True,
        )

    # 3. The thesis resolved.
    if episode_status == "reverted":
        return ExitDecision(
            ExitReason.REVERTED,
            True,
            "the tracked episode reverted; the determinant no longer violates",
        )

    # 4. It has not resolved in a reasonable time.
    held = position.held_minutes(now)
    if held >= policy.max_hold_minutes:
        return ExitDecision(
            ExitReason.TIME_STOP,
            True,
            f"held {held:.0f} min without reverting; limit is "
            f"{policy.max_hold_minutes} min",
        )

    return ExitDecision(
        ExitReason.HOLD,
        False,
        f"held {held:.0f} min, episode status {episode_status or 'unknown'}",
    )


def build_close_legs(position: OpenPosition) -> list[dict]:
    """Invert the entry into a single closing multi-leg order.

    Sent as one order, never as two. Legging out would leave the short
    uncovered between fills - precisely the exposure the covered structure was
    chosen to avoid.
    """
    return [
        {
            "symbol": position.long_symbol,
            "side": Side.SELL.value,
            "ratio_qty": str(position.qty),
            "position_intent": "sell_to_close",
        },
        {
            "symbol": position.short_symbol,
            "side": Side.BUY.value,
            "ratio_qty": str(position.qty),
            "position_intent": "buy_to_close",
        },
    ]

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

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

from .position import Side

__all__ = [
    "ExitReason",
    "PositionRegistry",
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


class PositionRegistry:
    """What we actually hold, persisted across restarts.

    Kept separate from the episode tracker on purpose. An episode is a market
    observation; a position is an obligation. They diverge - an episode can
    revert while the closing order is still working, and a position can outlive
    the episode that justified it. Conflating them would let a reverted episode
    silently imply a flat book.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.positions: dict[str, OpenPosition] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                pos = OpenPosition(
                    episode_id=row["episode_id"],
                    underlying=row["underlying"],
                    denomination=row["denomination"],
                    order_id=row["order_id"],
                    opened_at=datetime.fromisoformat(row["opened_at"]),
                    long_symbol=row["long_symbol"],
                    short_symbol=row["short_symbol"],
                    long_expiry=date.fromisoformat(row["long_expiry"]),
                    short_expiry=date.fromisoformat(row["short_expiry"]),
                    entry_long_price=float(row.get("entry_long_price") or 0),
                    entry_short_price=float(row.get("entry_short_price") or 0),
                    qty=int(row.get("qty") or 1),
                    close_order_id=row.get("close_order_id"),
                    close_reason=row.get("close_reason"),
                )
            except (KeyError, ValueError):
                continue
            if row.get("closed_at"):
                pos.closed_at = datetime.fromisoformat(row["closed_at"])
            self.positions[pos.episode_id] = pos

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(p.to_record(), default=str)
            for p in sorted(self.positions.values(), key=lambda x: x.opened_at)
        ]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def add(self, position: OpenPosition) -> None:
        self.positions[position.episode_id] = position
        self._flush()

    def close(
        self, episode_id: str, close_order_id: str | None, reason: str, when: datetime
    ) -> None:
        pos = self.positions.get(episode_id)
        if pos is None:
            return
        pos.closed_at = when
        pos.close_order_id = close_order_id
        pos.close_reason = reason
        self._flush()

    def open_positions(self) -> list[OpenPosition]:
        return [p for p in self.positions.values() if p.is_open]

    def held_symbols(self) -> set[str]:
        out: set[str] = set()
        for p in self.open_positions():
            out.add(p.long_symbol)
            out.add(p.short_symbol)
        return out


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

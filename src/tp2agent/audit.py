"""The append-only record of every decision the agent made, and why.

This is the evidence layer. It exists so that after the fact we can answer, for
any moment, what the agent saw and what it did about it - including the far more
common case where it did nothing.

Three properties are deliberate:

APPEND-ONLY
    Records are only ever added. The file is opened in "a" mode and nothing in
    this module truncates, rewrites or deletes. An audit log that can be edited
    after a loss is not evidence, and the discipline is worth more than the
    disk space.

DECISIONS, NOT JUST ORDERS
    orders.jsonl records what was sent. That is a survivorship-biased view: it
    cannot show the rectangle that was detected and refused, which is most of
    them. Every considered candidate gets a record here whether or not it
    became an order, so an abstention is as legible as a fill.

EVIDENCE, NOT CONCLUSIONS
    A record carries the quotes, the determinant, the theory category, the
    selector's scores and every risk gate that ran - passes as well as
    failures. It stores what was true, not a summary of it. The narrator turns
    these into prose; nothing turns prose back into a decision.

The narrator reads this file. It never writes to it, and the trading path never
reads what the narrator produced - see narrator.py.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = ["Outcome", "DecisionRecord", "AuditLog"]


class Outcome(str, Enum):
    """What actually happened to a candidate. Ordered from least to most action."""

    THEORY_BLOCKED = "theory_blocked"    # the gate could not certify no early exercise
    NOT_EXECUTABLE = "not_executable"    # no covered integer ratio exists
    ABSTAINED = "abstained"              # the selector declined to pick a denomination
    RISK_REJECTED = "risk_rejected"      # one or more deterministic gates refused
    ORDER_FAILED = "order_failed"        # submission was attempted and errored
    TRADED = "traded"                    # an order was accepted by the broker
    HELD = "held"                        # an open position was left open
    CLOSED = "closed"                    # an open position was closed

    @property
    def is_action(self) -> bool:
        return self in (Outcome.TRADED, Outcome.CLOSED)


@dataclass(frozen=True)
class DecisionRecord:
    """One decision, with the evidence that produced it.

    Frozen: a record describes something that already happened, so nothing
    downstream - the narrator least of all - may alter it after the fact.
    """

    ts: str
    underlying: str
    outcome: Outcome
    episode_key: str = ""
    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stage: str = ""                      # where in the pipeline this was settled
    reason: str = ""                     # one line, human-first
    theory_category: str = ""
    denomination: str = ""
    selector: dict[str, Any] = field(default_factory=dict)   # name, scores, abstained
    determinant: dict[str, Any] = field(default_factory=dict)  # lhs, rhs, severity
    quotes: dict[str, Any] = field(default_factory=dict)     # per-leg bid/ask at decision
    risk: dict[str, Any] = field(default_factory=dict)       # passes AND failures
    order: dict[str, Any] = field(default_factory=dict)
    broker: dict[str, Any] = field(default_factory=dict)     # order id, status, raw response
    extra: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "ts": self.ts,
            "underlying": self.underlying,
            "episode_key": self.episode_key,
            "outcome": self.outcome.value,
            "stage": self.stage,
            "reason": self.reason,
            "theory_category": self.theory_category,
            "denomination": self.denomination,
            "selector": dict(self.selector),
            "determinant": dict(self.determinant),
            "quotes": dict(self.quotes),
            "risk": dict(self.risk),
            "order": dict(self.order),
            "broker": dict(self.broker),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_record(cls, row: dict) -> "DecisionRecord":
        return cls(
            ts=row.get("ts", ""),
            underlying=row.get("underlying", ""),
            outcome=Outcome(row.get("outcome", "theory_blocked")),
            episode_key=row.get("episode_key", ""),
            decision_id=row.get("decision_id", ""),
            stage=row.get("stage", ""),
            reason=row.get("reason", ""),
            theory_category=row.get("theory_category", ""),
            denomination=row.get("denomination", ""),
            selector=row.get("selector") or {},
            determinant=row.get("determinant") or {},
            quotes=row.get("quotes") or {},
            risk=row.get("risk") or {},
            order=row.get("order") or {},
            broker=row.get("broker") or {},
            extra=row.get("extra") or {},
        )


class AuditLog:
    """Append-only JSONL sink for DecisionRecords.

    There is deliberately no update, no delete and no rewrite method. If a
    record turns out to be wrong the correction is a new record, exactly as it
    would be in a ledger.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, record: DecisionRecord) -> DecisionRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:   # append only, never "w"
            fh.write(json.dumps(record.to_record(), default=str) + "\n")
        return record

    def log(self, **kwargs) -> DecisionRecord:
        """Build and append in one call, stamping the time if absent."""
        kwargs.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        return self.append(DecisionRecord(**kwargs))

    def read(self) -> list[DecisionRecord]:
        if not self.path.exists():
            return []
        out: list[DecisionRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(DecisionRecord.from_record(json.loads(line)))
            except (json.JSONDecodeError, ValueError):
                continue          # a malformed line must not hide the rest
        return out

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.read():
            counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        return counts

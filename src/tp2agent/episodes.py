"""Episode lifecycle tracking: follow a violation until it reverts.

A single scan says a rectangle is violating. It does not say whether the anomaly
is a durable mispricing or a momentary quote artefact. The source study answers
that by consolidating consecutive observations of the same rectangle into an
*episode* and measuring how long it survives - finding a median duration of one
session and a maximum of fourteen, with the normalized violation rising sharply
on the signal day and then decaying toward zero.

This module reproduces that structure for a live scanner, at scan resolution
rather than daily resolution.

The mechanism that matters: a rectangle stops appearing in the detector's output
the moment it stops violating, so simply diffing successive scans cannot
distinguish "reverted" from "no longer measurable". Every tracked episode is
therefore *re-priced explicitly* from each new chain, whether or not it still
violates. That is what makes a reversion time observable rather than inferred.

Two files are written:

  episodes.csv      one row per episode, rewritten as it evolves - identity,
                    first/last seen, peak severity, status, reversion time.
  episode_path.csv  append-only: one row per (episode, scan), the severity path.
                    This is the event-time panel, and it is what a reversion
                    chart is drawn from.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .rectangles import ChainSnapshot, RectangleCandidate

__all__ = ["Episode", "EpisodeTracker", "remeasure"]

STATUS_ACTIVE = "active"
STATUS_REVERTED = "reverted"
STATUS_UNOBSERVABLE = "unobservable"


def episode_id(underlying: str, cand: RectangleCandidate) -> str:
    """Stable identity for a rectangle.

    Keyed on all four contracts rather than the near leg alone: two rectangles
    can share a short leg while differing elsewhere, and collapsing them would
    merge distinct anomalies into one episode.
    """
    raw = "|".join(
        [
            underlying,
            cand.A.symbol,
            cand.B.symbol,
            cand.C.symbol,
            cand.D.symbol,
        ]
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class Measurement:
    """One re-pricing of a tracked rectangle.

    Individual leg quotes are carried, not just the products. lhs and rhs are
    A_ask*B_ask and C_bid*D_bid, and the per-leg prices cannot be recovered from
    them - so without these fields an exit price is unrecoverable and no P&L can
    be computed from entry to reversion.
    """

    observable: bool
    lhs: float = 0.0
    rhs: float = 0.0
    severity: float = 0.0  # (rhs - lhs) / rhs; positive means violating
    violation_size: float = 0.0
    missing: tuple[str, ...] = ()
    quotes: dict[str, tuple[float, float]] = field(default_factory=dict)

    def leg(self, label: str, side: int) -> float:
        """Bid (side=0) or ask (side=1) for leg A/B/C/D; nan when unobserved."""
        pair = self.quotes.get(label)
        return pair[side] if pair else float("nan")

    @property
    def violating(self) -> bool:
        return self.observable and self.severity > 0


def remeasure(chain: ChainSnapshot, ep: "Episode") -> Measurement:
    """Re-price a tracked rectangle from a fresh chain.

    Looks the four contracts up by (expiry, strike) and recomputes the quote-side
    determinant. Returns `observable=False` when any leg has dropped out of the
    chain, so a missing quote is never mistaken for a reversion.
    """
    legs = {
        "A": (chain.calls.get(ep.T1, {}).get(ep.K1), ep.sym_A),
        "B": (chain.calls.get(ep.T2, {}).get(ep.K2), ep.sym_B),
        "C": (chain.calls.get(ep.T2, {}).get(ep.K1_adj), ep.sym_C),
        "D": (chain.calls.get(ep.T1, {}).get(ep.K2_adj), ep.sym_D),
    }
    missing = tuple(k for k, (opt, _) in legs.items() if opt is None)
    if missing:
        return Measurement(observable=False, missing=missing)

    A, B, C, D = (legs[k][0] for k in "ABCD")
    quotes = {
        label: (opt.quote.bid, opt.quote.ask)
        for label, opt in zip("ABCD", (A, B, C, D))
    }
    lhs = A.quote.ask * B.quote.ask
    rhs = C.quote.bid * D.quote.bid
    if rhs <= 0:
        return Measurement(observable=False, missing=("rhs<=0",), quotes=quotes)
    return Measurement(
        observable=True,
        lhs=lhs,
        rhs=rhs,
        severity=(rhs - lhs) / rhs,
        violation_size=rhs - lhs,
        quotes=quotes,
    )


def _dt(text: str) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _episode_from_row(row: dict) -> "Episode":
    """Rebuild an Episode from a persisted CSV row."""
    return Episode(
        episode_id=row["episode_id"],
        underlying=row["underlying"],
        T1=date.fromisoformat(row["T1"]),
        T2=date.fromisoformat(row["T2"]),
        K1=float(row["K1"]), K2=float(row["K2"]),
        K1_adj=float(row["K1_adj"]), K2_adj=float(row["K2_adj"]),
        sym_A=row["sym_A"], sym_B=row["sym_B"],
        sym_C=row["sym_C"], sym_D=row["sym_D"],
        theory_category=row.get("theory_category", ""),
        first_seen=_dt(row["first_seen"]),
        last_seen=_dt(row["last_seen"]),
        last_violating=_dt(row["last_violating"]),
        observations=int(row["observations"]),
        violating_observations=int(row["violating_observations"]),
        first_severity=float(row["first_severity"]),
        peak_severity=float(row["peak_severity"]),
        peak_at=_dt(row.get("peak_at", "")),
        last_severity=float(row["last_severity"]),
        status=row["status"],
        reverted_at=_dt(row.get("reverted_at", "")),
    )


@dataclass
class Episode:
    episode_id: str
    underlying: str
    T1: object
    T2: object
    K1: float
    K2: float
    K1_adj: float
    K2_adj: float
    sym_A: str
    sym_B: str
    sym_C: str
    sym_D: str
    theory_category: str

    first_seen: datetime
    last_seen: datetime
    last_violating: datetime
    observations: int = 1
    violating_observations: int = 1

    first_severity: float = 0.0
    peak_severity: float = 0.0
    peak_at: datetime | None = None
    last_severity: float = 0.0

    status: str = STATUS_ACTIVE
    reverted_at: datetime | None = None

    @property
    def duration_seconds(self) -> float:
        end = self.reverted_at or self.last_seen
        return (end - self.first_seen).total_seconds()

    @property
    def time_to_revert_seconds(self) -> float | None:
        if self.reverted_at is None:
            return None
        return (self.reverted_at - self.last_violating).total_seconds()

    def to_row(self) -> list:
        return [
            self.episode_id, self.underlying,
            self.T1.isoformat(), self.T2.isoformat(),
            self.K1, self.K2, self.K1_adj, self.K2_adj,
            self.sym_A, self.sym_B, self.sym_C, self.sym_D,
            self.theory_category,
            self.first_seen.isoformat(timespec="seconds"),
            self.last_seen.isoformat(timespec="seconds"),
            self.last_violating.isoformat(timespec="seconds"),
            self.observations, self.violating_observations,
            f"{self.first_severity:.6f}", f"{self.peak_severity:.6f}",
            self.peak_at.isoformat(timespec="seconds") if self.peak_at else "",
            f"{self.last_severity:.6f}",
            self.status,
            self.reverted_at.isoformat(timespec="seconds") if self.reverted_at else "",
            f"{self.duration_seconds:.0f}",
            f"{self.time_to_revert_seconds:.0f}" if self.time_to_revert_seconds is not None else "",
        ]


EPISODE_FIELDS = [
    "episode_id", "underlying", "T1", "T2", "K1", "K2", "K1_adj", "K2_adj",
    "sym_A", "sym_B", "sym_C", "sym_D", "theory_category",
    "first_seen", "last_seen", "last_violating",
    "observations", "violating_observations",
    "first_severity", "peak_severity", "peak_at", "last_severity",
    "status", "reverted_at", "duration_seconds", "time_to_revert_seconds",
]

PATH_FIELDS = [
    "episode_id", "ts", "event_index", "observable", "violating",
    "severity", "violation_size", "lhs", "rhs", "missing_legs",
    # Per-leg quotes at every observation. Required to compute an exit price:
    # lhs and rhs are products and the individual legs cannot be recovered
    # from them, so without these a backtest has entry prices and no exits.
    "A_bid", "A_ask", "B_bid", "B_ask", "C_bid", "C_ask", "D_bid", "D_ask",
]


class EpisodeTracker:
    """Follows every detected rectangle until it reverts.

    Reversion is declared only after `revert_after` consecutive non-violating
    observations, so a single stale or crossed quote does not close an episode
    that is still live.
    """

    def __init__(
        self, root: Path, underlying: str, revert_after: int = 2
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.underlying = underlying
        self.revert_after = revert_after
        self.episodes: dict[str, Episode] = {}
        self._non_violating_streak: dict[str, int] = {}
        self._event_index: dict[str, int] = {}
        self.path_file = self.root / "episode_path.csv"
        self.episode_file = self.root / "episodes.csv"
        if not self.path_file.exists():
            with self.path_file.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(PATH_FIELDS)
        self._load()

    # -- restart recovery --------------------------------------------------

    def _load(self) -> None:
        """Restore prior state from disk.

        `flush` rewrites episodes.csv wholesale from the in-memory registry, so a
        tracker that started empty would erase every episode recorded before a
        restart. State is therefore reloaded here, and the per-episode event
        index is recovered from the append-only path file so indices continue
        rather than restarting at zero.
        """
        if self.episode_file.exists():
            with self.episode_file.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        ep = _episode_from_row(row)
                    except (KeyError, ValueError):
                        continue
                    self.episodes[ep.episode_id] = ep
                    self._non_violating_streak[ep.episode_id] = 0

        if self.path_file.exists():
            with self.path_file.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    ep_id = row.get("episode_id")
                    try:
                        idx = int(row.get("event_index", 0))
                    except ValueError:
                        continue
                    if ep_id:
                        self._event_index[ep_id] = max(
                            self._event_index.get(ep_id, 0), idx + 1
                        )

    # -- persistence -------------------------------------------------------

    def _append_path(self, ep_id: str, ts: datetime, m: Measurement) -> None:
        idx = self._event_index.get(ep_id, 0)
        with self.path_file.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(
                [
                    ep_id, ts.isoformat(timespec="seconds"), idx,
                    int(m.observable), int(m.violating),
                    f"{m.severity:.6f}", f"{m.violation_size:.6f}",
                    f"{m.lhs:.6f}", f"{m.rhs:.6f}",
                    "|".join(m.missing),
                    *[
                        f"{m.leg(lbl, side):.6f}"
                        for lbl in "ABCD"
                        for side in (0, 1)
                    ],
                ]
            )
        self._event_index[ep_id] = idx + 1

    def flush(self) -> None:
        """Rewrite the episode state file. Small enough to rewrite each scan."""
        with self.episode_file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(EPISODE_FIELDS)
            for ep in sorted(
                self.episodes.values(), key=lambda e: e.first_seen
            ):
                writer.writerow(ep.to_row())

    # -- the scan step -----------------------------------------------------

    def observe(
        self,
        ts: datetime,
        chain: ChainSnapshot,
        detected: list[RectangleCandidate],
        categories: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """Fold one scan into the episode registry.

        Every currently-tracked episode is re-priced from `chain` regardless of
        whether it appears in `detected`, which is what makes reversion
        observable rather than inferred from absence.
        """
        categories = categories or {}
        stats = {"new": 0, "continuing": 0, "reverted": 0, "unobservable": 0}
        seen_now: set[str] = set()

        # 1. New and continuing episodes, from this scan's detections.
        for cand in detected:
            ep_id = episode_id(self.underlying, cand)
            seen_now.add(ep_id)
            severity = cand.normalized_severity
            existing = self.episodes.get(ep_id)

            if existing is None or existing.status == STATUS_REVERTED:
                ep = Episode(
                    episode_id=ep_id,
                    underlying=self.underlying,
                    T1=cand.T1, T2=cand.T2,
                    K1=cand.K1, K2=cand.K2,
                    K1_adj=cand.K1_adj, K2_adj=cand.K2_adj,
                    sym_A=cand.A.symbol, sym_B=cand.B.symbol,
                    sym_C=cand.C.symbol, sym_D=cand.D.symbol,
                    theory_category=categories.get(ep_id, ""),
                    first_seen=ts, last_seen=ts, last_violating=ts,
                    first_severity=severity,
                    peak_severity=severity, peak_at=ts,
                    last_severity=severity,
                )
                self.episodes[ep_id] = ep
                self._non_violating_streak[ep_id] = 0
                self._event_index.setdefault(ep_id, 0)
                stats["new"] += 1
            else:
                ep = existing
                ep.last_seen = ts
                ep.last_violating = ts
                ep.observations += 1
                ep.violating_observations += 1
                ep.last_severity = severity
                if severity > ep.peak_severity:
                    ep.peak_severity = severity
                    ep.peak_at = ts
                self._non_violating_streak[ep_id] = 0
                stats["continuing"] += 1

            self._append_path(
                ep_id, ts,
                Measurement(
                    True, cand.lhs, cand.rhs, severity, cand.violation_size,
                    quotes={
                        lbl: (leg.quote.bid, leg.quote.ask)
                        for lbl, leg in zip(
                            "ABCD", (cand.A, cand.B, cand.C, cand.D)
                        )
                    },
                ),
            )

        # 2. Re-price every active episode that did NOT appear this scan.
        for ep_id, ep in self.episodes.items():
            if ep_id in seen_now or ep.status != STATUS_ACTIVE:
                continue
            m = remeasure(chain, ep)
            ep.observations += 1
            ep.last_seen = ts
            self._append_path(ep_id, ts, m)

            if not m.observable:
                stats["unobservable"] += 1
                continue

            ep.last_severity = m.severity
            if m.severity > ep.peak_severity:
                ep.peak_severity = m.severity
                ep.peak_at = ts

            if m.violating:
                # Still violating, just below the detector's buffer this scan.
                ep.last_violating = ts
                ep.violating_observations += 1
                self._non_violating_streak[ep_id] = 0
                continue

            streak = self._non_violating_streak.get(ep_id, 0) + 1
            self._non_violating_streak[ep_id] = streak
            if streak >= self.revert_after:
                ep.status = STATUS_REVERTED
                ep.reverted_at = ts
                stats["reverted"] += 1

        self.flush()
        return stats

    # -- summary -----------------------------------------------------------

    def summary(self) -> dict:
        active = [e for e in self.episodes.values() if e.status == STATUS_ACTIVE]
        reverted = [e for e in self.episodes.values() if e.status == STATUS_REVERTED]
        durations = sorted(e.duration_seconds for e in reverted)
        median = durations[len(durations) // 2] if durations else 0.0
        return {
            "total": len(self.episodes),
            "active": len(active),
            "reverted": len(reverted),
            "median_duration_s": median,
            "max_duration_s": durations[-1] if durations else 0.0,
            "peak_severity": max(
                (e.peak_severity for e in self.episodes.values()), default=0.0
            ),
        }

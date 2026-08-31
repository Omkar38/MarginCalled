"""CSV persistence for scan results.

Three files, all append-only, all plain CSV so they open in anything:

  scans.csv       one row per scan: census, margin summary, timing.
  violations.csv  one row per detected violation, with every field needed to
                  reconstruct the rectangle and audit the decision.
  margins.csv     one row per scan holding a fixed-bin histogram of the
                  normalized TP2 margin, so the distribution can be charted over
                  time without storing every sample.

The margin is (rhs - lhs) / rhs for every rectangle that reaches the violation
test. A positive value is a violation. Storing the whole distribution - not only
the violations - is deliberate: with zero violations the distribution is the only
evidence available, and it distinguishes a genuinely clean market (margins
clustering near zero) from a synthetic feed (margins bounded away from zero).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

__all__ = ["MarginSummary", "ScanStore", "HIST_EDGES"]

# Fixed bins for the margin histogram. Dense near zero, because that is the only
# region where the distinction between "close to violating" and "violating"
# matters; the far tail only needs coarse coverage.
HIST_EDGES: tuple[float, ...] = (
    -8.0, -6.0, -4.0, -3.0, -2.0, -1.5, -1.0, -0.75, -0.50, -0.35,
    -0.25, -0.175, -0.125, -0.09, -0.06, -0.04, -0.025, -0.015, -0.01,
    -0.005, -0.002, 0.0, 0.002, 0.005, 0.01, 0.025, 0.05, 0.10,
)


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return float("nan")
    idx = min(len(values) - 1, max(0, int(q * (len(values) - 1))))
    return values[idx]


@dataclass
class MarginSummary:
    count: int = 0
    minimum: float = float("nan")
    p05: float = float("nan")
    p25: float = float("nan")
    median: float = float("nan")
    p75: float = float("nan")
    p95: float = float("nan")
    maximum: float = float("nan")
    violations: int = 0
    near_misses: int = 0  # margin > -0.01 but not violating
    histogram: tuple[int, ...] = ()

    @classmethod
    def from_margins(cls, margins: Sequence[float]) -> "MarginSummary":
        if not margins:
            return cls(histogram=tuple([0] * (len(HIST_EDGES) - 1)))
        ordered = sorted(margins)
        counts = [0] * (len(HIST_EDGES) - 1)
        for value in ordered:
            for i in range(len(HIST_EDGES) - 1):
                if HIST_EDGES[i] <= value < HIST_EDGES[i + 1]:
                    counts[i] += 1
                    break
            else:
                if value >= HIST_EDGES[-1]:
                    counts[-1] += 1
        return cls(
            count=len(ordered),
            minimum=ordered[0],
            p05=_pct(ordered, 0.05),
            p25=_pct(ordered, 0.25),
            median=_pct(ordered, 0.50),
            p75=_pct(ordered, 0.75),
            p95=_pct(ordered, 0.95),
            maximum=ordered[-1],
            violations=sum(1 for v in ordered if v > 0),
            near_misses=sum(1 for v in ordered if -0.01 < v <= 0),
            histogram=tuple(counts),
        )


SCAN_FIELDS = [
    "ts", "feed", "spot", "quote_age_s", "duration_s",
    "expiry_pairs", "rectangles_considered", "no_forward",
    "adjusted_strike_unlisted", "leg_missing", "leg_unusable",
    "strike_gap_too_wide", "coverage_ratio_too_wide", "no_violation",
    "below_tick_bound", "detected", "episodes",
    "margin_count", "margin_min", "margin_p05", "margin_p25", "margin_median",
    "margin_p75", "margin_p95", "margin_max", "violations", "near_misses",
]

VIOLATION_FIELDS = [
    "ts", "feed", "spot", "episode_key", "T1", "T2", "K1", "K2",
    "K1_adj", "K2_adj", "F_T1", "F_T2",
    "sym_A", "sym_B", "sym_C", "sym_D",
    "A_bid", "A_ask", "B_bid", "B_ask", "C_bid", "C_ask", "D_bid", "D_ask",
    "lhs", "rhs", "violation_size", "normalized_severity",
    "tick_bound", "coverage_ratio", "theory_category", "theory_tradable",
]


class ScanStore:
    """Append-only CSV writer. Headers are written once, on creation."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.scans = self.root / "scans.csv"
        self.violations = self.root / "violations.csv"
        self.margins = self.root / "margins.csv"
        self._ensure(self.scans, SCAN_FIELDS)
        self._ensure(self.violations, VIOLATION_FIELDS)
        self._ensure(
            self.margins,
            ["ts", "feed"] + [f"bin_{i}" for i in range(len(HIST_EDGES) - 1)],
        )

    @staticmethod
    def _ensure(path: Path, fields: list[str]) -> None:
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(fields)

    @staticmethod
    def _append(path: Path, row: list) -> None:
        with path.open("a", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(row)

    def record_scan(
        self,
        ts: datetime,
        feed: str,
        spot: float,
        quote_age_s: float,
        duration_s: float,
        census: dict,
        episodes: int,
        summary: MarginSummary,
    ) -> None:
        self._append(
            self.scans,
            [
                ts.isoformat(timespec="seconds"), feed, f"{spot:.2f}",
                f"{quote_age_s:.1f}", f"{duration_s:.1f}",
                census.get("expiry_pairs", 0),
                census.get("rectangles_considered", 0),
                census.get("no_forward", 0),
                census.get("adjusted_strike_unlisted", 0),
                census.get("leg_missing", 0),
                census.get("leg_unusable", 0),
                census.get("strike_gap_too_wide", 0),
                census.get("coverage_ratio_too_wide", 0),
                census.get("no_violation", 0),
                census.get("below_tick_bound", 0),
                census.get("detected", 0),
                episodes,
                summary.count,
                f"{summary.minimum:.6f}", f"{summary.p05:.6f}",
                f"{summary.p25:.6f}", f"{summary.median:.6f}",
                f"{summary.p75:.6f}", f"{summary.p95:.6f}",
                f"{summary.maximum:.6f}",
                summary.violations, summary.near_misses,
            ],
        )
        self._append(
            self.margins,
            [ts.isoformat(timespec="seconds"), feed, *summary.histogram],
        )

    def record_violation(
        self, ts: datetime, feed: str, spot: float, cand, category: str, tradable: bool
    ) -> None:
        self._append(
            self.violations,
            [
                ts.isoformat(timespec="seconds"), feed, f"{spot:.2f}",
                cand.episode_key, cand.T1.isoformat(), cand.T2.isoformat(),
                cand.K1, cand.K2, cand.K1_adj, cand.K2_adj,
                f"{cand.F_T1:.4f}", f"{cand.F_T2:.4f}",
                cand.A.symbol, cand.B.symbol, cand.C.symbol, cand.D.symbol,
                cand.A.quote.bid, cand.A.quote.ask,
                cand.B.quote.bid, cand.B.quote.ask,
                cand.C.quote.bid, cand.C.quote.ask,
                cand.D.quote.bid, cand.D.quote.ask,
                f"{cand.lhs:.6f}", f"{cand.rhs:.6f}",
                f"{cand.violation_size:.6f}", f"{cand.normalized_severity:.6f}",
                f"{cand.tick_bound:.6f}", f"{cand.coverage_ratio:.4f}",
                category, tradable,
            ],
        )

    # -- reading, for the report ------------------------------------------

    def read_scans(self) -> list[dict]:
        if not self.scans.exists():
            return []
        with self.scans.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def read_violations(self) -> list[dict]:
        if not self.violations.exists():
            return []
        with self.violations.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def read_margin_histograms(self) -> list[dict]:
        if not self.margins.exists():
            return []
        with self.margins.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

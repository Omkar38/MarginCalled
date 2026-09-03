#!/usr/bin/env python3
"""Replay today's reverted episodes and price them under both sizings.

The agent trades unit-sized (1 contract per leg) because the account is options
level 3 and the study's weights are always short-heavy - short C(K~1,T2) shares
against long C(K2,T2), and C exceeds B on every rectangle, so the surplus shorts
are naked calls the broker refuses. This replays the same signals under the
weights the study specifies, to measure what that constraint costs.

Entry is the first violating observation, crossing the spread: buy the long leg
at its ask, sell the short leg at its bid. Exit is the observation at which the
episode reverted, crossing back: sell the long at its bid, buy the short at its
ask. Nothing is marked to mid, so the round-trip spread is paid in full - the
same treatment the live agent gets.

    python3 scripts/replay_pnl.py --underlying SPY
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

MULT = 100.0
LEGS = {"T1": ("A", "D"), "K2": ("B", "D")}


def _f(row, key):
    try:
        v = float(row.get(key, "") or "nan")
        return v if v == v else None
    except ValueError:
        return None


def load_paths(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    with path.open() as fh:
        for row in csv.DictReader(fh):
            out[row["episode_id"]].append(row)
    for rows in out.values():
        rows.sort(key=lambda r: r.get("ts", ""))
    return out


def replay(underlying: str, structure: str, day: str | None) -> None:
    base = Path("data") / underlying
    episodes = [r for r in csv.DictReader((base / "episodes.csv").open())
                if r.get("status") == "reverted"
                and (day is None or r.get("first_seen", "").startswith(day))]
    paths = load_paths(base / "episode_path.csv")
    long_leg, short_leg = LEGS[structure]

    unit, weighted, ratios, skipped = [], [], [], 0
    for ep in episodes:
        rows = paths.get(ep["episode_id"]) or []
        entry = next((r for r in rows if r.get("violating") in ("True", "true", "1")), None)
        exit_ = next((r for r in reversed(rows)
                      if r.get("violating") in ("False", "false", "0")
                      and r.get("observable") in ("True", "true", "1")), None)
        if entry is None or exit_ is None:
            skipped += 1
            continue
        la, sb = _f(entry, f"{long_leg}_ask"), _f(entry, f"{short_leg}_bid")
        lb, sa = _f(exit_, f"{long_leg}_bid"), _f(exit_, f"{short_leg}_ask")
        if None in (la, sb, lb, sa):
            skipped += 1
            continue

        # Per-contract P&L, crossing the spread both ways.
        long_pnl = (lb - la) * MULT
        short_pnl = (sb - sa) * MULT
        unit.append(long_pnl + short_pnl)

        # The study's weights: long C(K2,T2)=B_mid units, short C(K~1,T2)=C_mid.
        b_mid = (_f(entry, "B_bid"), _f(entry, "B_ask"))
        c_mid = (_f(entry, "C_bid"), _f(entry, "C_ask"))
        if None in b_mid or None in c_mid:
            continue
        qty_long = (b_mid[0] + b_mid[1]) / 2 if structure == "T1" else None
        if structure == "K2":
            a_mid = (_f(entry, "A_bid"), _f(entry, "A_ask"))
            if None in a_mid:
                continue
            qty_long = (a_mid[0] + a_mid[1]) / 2
        qty_short = (c_mid[0] + c_mid[1]) / 2
        if not qty_long or qty_long <= 0:
            continue
        weighted.append(qty_long * long_pnl + qty_short * short_pnl)
        ratios.append(qty_short / qty_long)

    def show(name, vals):
        if not vals:
            print(f"  {name:<26} no episodes priced")
            return
        wins = sum(1 for v in vals if v > 0)
        print(f"  {name:<26} n={len(vals):<5} total {sum(vals):>12,.2f}  "
              f"mean {statistics.mean(vals):>9,.2f}  median {statistics.median(vals):>9,.2f}  "
              f"win {100*wins/len(vals):>4.0f}%")

    print(f"{underlying} {structure} — reverted episodes"
          f"{' on ' + day if day else ''}")
    print(f"  episodes reverted: {len(episodes)}   priced: {len(unit)}   skipped: {skipped}")
    print()
    show("unit sizing (traded)", unit)
    show("study weights", weighted)
    if ratios:
        rs = sorted(ratios)
        print()
        print(f"  short:long ratio the study wants — median {statistics.median(rs):.2f}, "
              f"max {rs[-1]:.2f}, min {rs[0]:.2f}")
        print(f"  every ratio above 1.0 needs naked shorts, which options level 3 refuses.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="SPY")
    ap.add_argument("--structure", default="T1", choices=sorted(LEGS))
    ap.add_argument("--day", default=None, help="YYYY-MM-DD; default all")
    a = ap.parse_args()
    replay(a.underlying, a.structure, a.day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

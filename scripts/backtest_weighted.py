#!/usr/bin/env python3
"""Backtest today's signals through the live mechanism, at the study's weights.

The live agent applies, in order: the early-exercise theory gate, the execution
screens (coverage ratio, cheapest leg, relative spread), re-validation that the
violation still holds, and the risk caps. It then sizes 1:1, because options
level 3 refuses the study's short-heavy weights.

This replays the same episodes through the same filters and changes only the
sizing. The study says the weights "may have to be rescaled and adjusted to the
closest integers", so they are normalised to make the long leg one contract and
the short leg rounded to the nearest whole number - the honest integer version
of the theory, and the thing the broker rejected.

Entry crosses the spread at the first violating observation; exit crosses back
at reversion. Nothing marks to mid.

    python3 scripts/backtest_weighted.py --underlying SPY
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

MULT = 100.0
SIZING = "weighted"


def _f(row, key):
    try:
        v = float(row.get(key, "") or "nan")
        return v if v == v else None
    except ValueError:
        return None


def run(underlying: str, day: str, equity: float, max_loss_pct: float,
        max_positions: int, coverage_cap: float, min_leg: float) -> None:
    base = Path("data") / underlying

    # The screens the live agent actually applied, read from what it recorded.
    gate_ok, screens = {}, {}
    for r in csv.DictReader((base / "violations.csv").open()):
        if day and not r["ts"].startswith(day):
            continue
        k = r["episode_key"]
        gate_ok[k] = r.get("theory_tradable") == "True"
        screens[k] = (_f(r, "coverage_ratio"), _f(r, "min_leg_mid"))

    paths = defaultdict(list)
    for r in csv.DictReader((base / "episode_path.csv").open()):
        paths[r["episode_id"]].append(r)
    for v in paths.values():
        v.sort(key=lambda r: r.get("ts", ""))

    eps = [r for r in csv.DictReader((base / "episodes.csv").open())
           if r.get("status") == "reverted" and (not day or r.get("first_seen", "").startswith(day))]

    cap = equity * max_loss_pct
    rows, rejected = [], defaultdict(int)
    for ep in eps:
        key = ep.get("sym_D") or ""
        if not gate_ok.get(key, False):
            rejected["theory gate"] += 1
            continue
        cov, leg = screens.get(key, (None, None))
        if cov is None or cov > coverage_cap:
            rejected["coverage ratio"] += 1
            continue
        if leg is None or leg < min_leg:
            rejected["cheapest leg"] += 1
            continue

        obs = paths.get(ep["episode_id"]) or []
        entry = next((r for r in obs if r.get("violating") in ("True", "true", "1")), None)
        exit_ = next((r for r in reversed(obs)
                      if r.get("violating") in ("False", "false", "0")
                      and r.get("observable") in ("True", "true", "1")), None)
        if entry is None or exit_ is None:
            rejected["no entry/exit quote"] += 1
            continue
        la, sb = _f(entry, "A_ask"), _f(entry, "D_bid")
        lb, sa = _f(exit_, "A_bid"), _f(exit_, "D_ask")
        bb, ba = _f(entry, "B_bid"), _f(entry, "B_ask")
        cb, ca = _f(entry, "C_bid"), _f(entry, "C_ask")
        if None in (la, sb, lb, sa, bb, ba, cb, ca):
            rejected["no entry/exit quote"] += 1
            continue

        # Study weights, normalised so the long leg is one contract and the
        # short leg is the nearest whole number - the paper's own instruction.
        b_mid, c_mid = (bb + ba) / 2, (cb + ca) / 2
        if b_mid <= 0:
            rejected["no entry/exit quote"] += 1
            continue
        n_long = 1
        n_short = 1 if SIZING == "unit" else max(1, round(c_mid / b_mid))

        debit = la * n_long - sb * n_short
        # Max loss for a short-heavy call spread is unbounded above the short
        # strike. The risk gate sizes on the defined part plus the uncovered
        # width to the next strike, which is the least it could be.
        k_long, k_short = _f({"v": ep["K1"]}, "v"), _f({"v": ep["K2_adj"]}, "v")
        width = (k_short - k_long) if (k_long and k_short) else 0.0
        naked = max(n_short - n_long, 0)
        est_max_loss = (max(debit, 0.0) + width * n_long + width * naked) * MULT
        if est_max_loss > cap:
            rejected[f"max loss > {max_loss_pct:.1%} equity"] += 1
            continue

        pnl = ((lb - la) * n_long + (sb - sa) * n_short) * MULT
        rows.append((pnl, n_short, est_max_loss))

    label = "study weights" if SIZING == "weighted" else "unit sizing (1:1)"
    print(f"{underlying} T1 — {label} through the live filters"
          f"{' on ' + day if day else ''}")
    print(f"  reverted episodes considered : {len(eps)}")
    for k, v in sorted(rejected.items(), key=lambda x: -x[1]):
        print(f"     rejected, {k:<28} {v}")
    print(f"  survived every filter        : {len(rows)}")
    if not rows:
        print("\n  nothing survives. At the study's weights the position is short-heavy,")
        print("  so its max loss includes uncovered width and the per-trade cap refuses it.")
        return
    pnl = [r[0] for r in rows]
    wins = sum(1 for v in pnl if v > 0)
    print()
    print(f"  total {sum(pnl):>14,.2f}   mean {statistics.mean(pnl):>10,.2f}   "
          f"median {statistics.median(pnl):>10,.2f}   win {100*wins/len(pnl):.0f}%")
    print(f"  worst {min(pnl):>14,.2f}   best {max(pnl):>10,.2f}")
    ns = [r[1] for r in rows]
    print(f"  short contracts per 1 long: median {statistics.median(ns):.0f}, max {max(ns)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="SPY")
    ap.add_argument("--day", default="2026-09-03")
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--max-loss-pct", type=float, default=0.025)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--coverage", type=float, default=15.0)
    ap.add_argument("--min-leg", type=float, default=0.03)
    ap.add_argument("--sizing", default="weighted", choices=("weighted", "unit"),
                    help="weighted = the study's integer-rounded weights; "
                         "unit = 1:1, what the broker permits")
    a = ap.parse_args()
    global SIZING
    SIZING = a.sizing
    run(a.underlying, a.day, a.equity, a.max_loss_pct, a.max_positions,
        a.coverage, a.min_leg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

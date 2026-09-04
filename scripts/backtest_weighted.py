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
MAX_SCALE = 10          # liquidity ceiling on how far 1:1 may be scaled
DENOM = "T1"            # which denomination to trade: T1 (A,D) or K2 (B,D)


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
        # Exit at the FIRST reversion after entry, never the last observation.
        #
        # This previously scanned reversed(obs), taking the last non-violating
        # row in the whole path - a look-ahead. It exited at whatever price
        # happened to be best hours later, and reported a profit where the live
        # account lost money on the same episodes. The agent cannot see the
        # future; it closes on the first reversion its tracker confirms.
        entry_i = next((i for i, r in enumerate(obs)
                        if r.get("violating") in ("True", "true", "1")), None)
        entry = obs[entry_i] if entry_i is not None else None
        exit_ = None
        if entry_i is not None:
            exit_ = next((r for r in obs[entry_i + 1:]
                          if r.get("violating") in ("False", "false", "0")
                          and r.get("observable") in ("True", "true", "1")), None)
        if entry is None or exit_ is None:
            rejected["no entry/exit quote"] += 1
            continue
        long_sym = "A" if DENOM == "T1" else "B"
        la, sb = _f(entry, f"{long_sym}_ask"), _f(entry, "D_bid")
        lb, sa = _f(exit_, f"{long_sym}_bid"), _f(exit_, "D_ask")
        bb, ba = _f(entry, "B_bid"), _f(entry, "B_ask")
        cb, ca = _f(entry, "C_bid"), _f(entry, "C_ask")
        if None in (la, sb, lb, sa, bb, ba, cb, ca):
            rejected["no entry/exit quote"] += 1
            continue

        # Study weights, normalised so the long leg is one contract and the
        # short leg is the nearest whole number - the paper's own instruction.
        # Table 5.1. T1: long A in C(K2,T2)=B units, short D in C(K~1,T2)=C units.
        # K2: long B in C(K1,T1)=A units, short D in the same C units.
        if DENOM == "T1":
            b_mid = (bb + ba) / 2
        else:
            ab, aa = _f(entry, "A_bid"), _f(entry, "A_ask")
            if None in (ab, aa):
                rejected["no entry/exit quote"] += 1
                continue
            b_mid = (ab + aa) / 2
        c_mid = (cb + ca) / 2
        if b_mid <= 0:
            rejected["no entry/exit quote"] += 1
            continue
        # Three sizings.
        #   unit      1:1, one contract a leg - what the agent traded
        #   weighted  the study's ratio, rounded to whole contracts. The study
        #             says positions "may have to be rescaled and adjusted to
        #             the closest integers"; a market cannot sell 1.74 contracts
        #   scaled    1:1, multiplied up until the risk cap binds. The RATIO is
        #             what the broker forbids, never the SIZE, and at a median
        #             max loss of $4 against a $2,500 cap the agent was using
        #             about 1/625th of the budget available to it
        n_long = 1
        if SIZING == "weighted":
            n_short = max(1, round(c_mid / b_mid))
        else:
            n_short = 1

        debit = la * n_long - sb * n_short
        # Max loss for a short-heavy call spread is unbounded above the short
        # strike. The risk gate sizes on the defined part plus the uncovered
        # width to the next strike, which is the least it could be.
        k_long, k_short = _f({"v": ep["K1"]}, "v"), _f({"v": ep["K2_adj"]}, "v")
        width = (k_short - k_long) if (k_long and k_short) else 0.0
        naked = max(n_short - n_long, 0)
        # A covered 1:1 call spread cannot lose more than its debit - the long
        # lower strike caps the short higher one. Only the UNCOVERED excess adds
        # width-scaled risk. Charging width to the covered part overstated the
        # 1:1 max loss by two orders of magnitude ($806 against a real $4).
        est_max_loss = (max(debit, 0.0) + width * naked) * MULT
        if est_max_loss > cap:
            rejected[f"max loss > {max_loss_pct:.1%} equity"] += 1
            continue

        scale = 1
        if SIZING == "scaled" and est_max_loss > 0:
            by_risk = int(cap // est_max_loss)
            # Liquidity, not just risk. These are penny options quoting one or
            # two lots; assuming a 600-contract fill because the risk budget
            # allows it would be fantasy. MAX_SCALE is a blunt stand-in for
            # displayed size and is the binding constraint in practice.
            scale = max(1, min(by_risk, MAX_SCALE))
        pnl = ((lb - la) * n_long + (sb - sa) * n_short) * MULT * scale
        rows.append((pnl, n_short, est_max_loss * scale, scale))

    label = {"weighted": "study weights (integer-rounded)",
             "unit": "unit 1:1 (as traded)",
             "scaled": "1:1 scaled to the risk cap"}[SIZING]
    print(f"{underlying} {DENOM} — {label} through the live filters"
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
    sc = [r[3] for r in rows]
    if max(sc) > 1:
        print(f"  size multiple applied     : median {statistics.median(sc):.0f}x, max {max(sc)}x")
    ml = [r[2] for r in rows]
    print(f"  max loss per trade        : median ${statistics.median(ml):,.0f}, "
          f"cap ${cap:,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--underlying", default="SPY")
    ap.add_argument("--day", default="2026-09-03")
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--max-loss-pct", type=float, default=0.025)
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--coverage", type=float, default=15.0)
    ap.add_argument("--min-leg", type=float, default=0.03)
    ap.add_argument("--denom", default="T1", choices=("T1","K2"),
                    help="T1 trades legs (A,D); K2 trades legs (B,D)")
    ap.add_argument("--max-scale", type=int, default=10,
                    help="liquidity ceiling for --sizing scaled (default 10)")
    ap.add_argument("--sizing", default="weighted",
                    choices=("weighted", "unit", "scaled"),
                    help="weighted = study ratio rounded to integers; "
                         "unit = 1:1 single contract (what was traded); "
                         "scaled = 1:1 multiplied up to the risk cap")
    a = ap.parse_args()
    global SIZING, MAX_SCALE, DENOM
    SIZING = a.sizing
    MAX_SCALE = a.max_scale
    DENOM = a.denom
    run(a.underlying, a.day, a.equity, a.max_loss_pct, a.max_positions,
        a.coverage, a.min_leg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

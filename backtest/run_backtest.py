#!/usr/bin/env python3
"""Run the T1/K2 backtest over captured violations.

    python3 backtest/run_backtest.py                       # both underlyings
    python3 backtest/run_backtest.py --underlying SPX
    python3 backtest/run_backtest.py --sizing ratio        # paper weights
    python3 backtest/run_backtest.py --terminal-price 765  # settle expiries

Reads data/<UNDERLYING>/violations.csv and episode_path.csv; writes per-trade
rows to backtest/results/.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (  # noqa: E402
    BacktestConfig,
    Denomination,
    ExitMode,
    Sizing,
    run,
    summarise,
)


def _rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def main() -> int:
    ap = argparse.ArgumentParser(description="T1/K2 backtest on captured violations.")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--underlying", action="append",
                    help="repeatable; default SPY and SPX")
    ap.add_argument("--sizing", choices=[s.value for s in Sizing],
                    default=Sizing.CAPPED.value)
    ap.add_argument("--exit", choices=[m.value for m in ExitMode],
                    default=ExitMode.REVERSION.value)
    ap.add_argument("--terminal-price", type=float, default=None,
                    help="underlying level for expiry settlement")
    ap.add_argument("--cost", type=float, default=0.0,
                    help="per contract per side; Alpaca paper is 0")
    ap.add_argument("--out", type=Path, default=Path("backtest/results"))
    args = ap.parse_args()

    cfg = BacktestConfig(
        sizing=Sizing(args.sizing),
        exit_mode=ExitMode(args.exit),
        cost_per_contract_side=args.cost,
    )
    underlyings = args.underlying or ["SPY", "SPX"]
    args.out.mkdir(parents=True, exist_ok=True)

    print("TP2 BACKTEST — T1 and K2 denominations")
    print(f"  sizing    : {cfg.sizing.value}"
          f"{'  (paper weights; not executable)' if cfg.sizing is Sizing.RATIO else '  (1:1, executable)'}")
    print(f"  exit      : {cfg.exit_mode.value}")
    print(f"  cost/side : ${cfg.cost_per_contract_side}")

    all_trades = []
    for u in underlyings:
        d = args.data_dir / u
        if not (d / "violations.csv").exists():
            print(f"\n  {u}: no data")
            continue
        trades = run(d, u, cfg, args.terminal_price)
        all_trades.extend(trades)

        _rule(f"{u}")
        for denom in (Denomination.T1, Denomination.K2):
            subset = [t for t in trades if t.denomination is denom]
            s = summarise(subset)
            print(f"  {denom.value}: {s['trades']} trades, {s['resolved']} resolved, "
                  f"{s['unresolved']} unresolved")
            if s["resolved"]:
                print(f"       hit rate {s['hit_rate']:.1%}  "
                      f"total ${s['total_pnl']:,.2f}  "
                      f"mean ${s['mean_pnl']:,.2f}  "
                      f"median hold {s['median_hold_s']/60:.1f} min")
                if s["settled_at_expiry"]:
                    print(f"       settled at expiry: {s['settled_at_expiry']}")
            else:
                notes = {t.note for t in subset if t.note}
                for n in list(notes)[:2]:
                    print(f"       {n}")

        # Only completed trades go in the results file. A trade with no exit has
        # no P&L, and mixing the two makes every aggregate ambiguous - you cannot
        # tell a real zero from a missing one. Unresolved trades are written
        # alongside with the reason, so nothing is discarded silently.
        stem = f"{u}_{cfg.sizing.value}_{cfg.exit_mode.value}"
        resolved = [t for t in trades if t.resolved]
        unresolved = [t for t in trades if not t.resolved]

        if resolved:
            out = args.out / f"{stem}.csv"
            rows = [t.to_row() for t in resolved]
            with out.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            print(f"\n  -> {out}  ({len(resolved)} completed trades)")
        else:
            print(f"\n  -> no completed trades for {u}; nothing written")

        if unresolved:
            out_u = args.out / f"{stem}_unresolved.csv"
            rows = [t.to_row() for t in unresolved]
            with out_u.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            reasons: dict[str, int] = {}
            for t in unresolved:
                key = t.note or "no exit observation available"
                reasons[key] = reasons.get(key, 0) + 1
            print(f"  -> {out_u}  ({len(unresolved)} excluded)")
            for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
                print(f"       {n:4d}  {reason[:90]}")

    _rule("OVERALL")
    s = summarise(all_trades)
    print(f"  {s['trades']} trades, {s['resolved']} resolved, "
          f"{s['unresolved']} unresolved")
    if s["resolved"]:
        print(f"  hit rate {s['hit_rate']:.1%}   total ${s['total_pnl']:,.2f}   "
              f"mean ${s['mean_pnl']:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

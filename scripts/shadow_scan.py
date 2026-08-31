#!/usr/bin/env python3
"""One full pipeline pass over the live chain. Places no orders, ever.

chain -> rectangles -> theory gate -> position -> risk gates

Prints a census at every stage. That census is the point: it says how many
rectangles survive each screen, which is what you need before deciding whether
the thresholds are set sensibly for a real session.

SAFETY. This script imports nothing that can submit an order. The Alpaca client is
read-only (GET only, live hosts refused), and `risk.evaluate` is called purely to
report what it *would* decide. Nothing is sent.

USAGE.
    python3 scripts/shadow_scan.py                 # default expiries
    python3 scripts/shadow_scan.py --json out.json # also write an audit record
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.alpaca import AlpacaDataClient, AlpacaError  # noqa: E402
from tp2agent.position import PositionConfig, Structure, build_position  # noqa: E402
from tp2agent.rectangles import (  # noqa: E402
    RectangleConfig,
    build_rectangles,
    dedupe_episodes,
)
from tp2agent.risk import AccountState, RiskLimits, evaluate  # noqa: E402
from tp2agent.theory_gate import Dividend, Rectangle, Contract, classify  # noqa: E402

DEFAULT_EXPIRIES = [date(2026, 10, 16), date(2026, 11, 20), date(2026, 12, 18)]

# UNVERIFIED. SPY typically goes ex-dividend on the third Friday of the quarter-end
# month. Confirm the announced date and amount before trusting any classification:
# a wrong ex-date silently changes every theory decision.
SPY_DIVIDENDS = [Dividend(date(2026, 9, 18), 1.80)]
SHORT_RATE = 0.045


def _rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def _to_theory_rectangle(cand) -> Rectangle:
    """Adapt a detected candidate to the theory gate's input shape."""
    return Rectangle(
        signal_date=cand.signal_date,
        A=Contract("A", cand.A.strike, cand.A.expiry, cand.A.quote.bid, cand.A.quote.ask),
        B=Contract("B", cand.B.strike, cand.B.expiry, cand.B.quote.bid, cand.B.quote.ask),
        C=Contract("C", cand.C.strike, cand.C.expiry, cand.C.quote.bid, cand.C.quote.ask),
        D=Contract("D", cand.D.strike, cand.D.expiry, cand.D.quote.bid, cand.D.quote.ask),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only TP2 shadow scan.")
    ap.add_argument("--json", type=Path, help="write an audit record here")
    ap.add_argument("--max-spread", type=float, default=0.50)
    ap.add_argument("--buffer", type=float, default=0.02)
    ap.add_argument("--coverage", type=float, default=1.25)
    args = ap.parse_args()

    print("TP2 SHADOW SCAN — READ ONLY, no orders are placed")
    print(f"started : {datetime.now().isoformat(timespec='seconds')}")

    try:
        client = AlpacaDataClient()
    except AlpacaError as exc:
        print(f"\n{exc}")
        return 2

    record: dict = {"started": datetime.now().isoformat(timespec="seconds")}

    # -- feed and account ---------------------------------------------------
    _rule("FEED")
    try:
        feed = client.resolve_feed()
    except AlpacaError as exc:
        print(f"  {exc}")
        return 1
    print(f"  mode: {feed.label}")
    record["feed"] = feed.feed
    if not feed.is_live:
        print(
            "  WARNING: indicative quotes are Alpaca's derivatives of OPRA and are\n"
            "  documented as unsuitable for live trading decisions. Detections below\n"
            "  measure the derivation, not the market. Do not tune thresholds on them."
        )

    account = client.account()
    equity = float(account.get("equity", 0) or 0)
    print(f"  equity ${equity:,.2f}   options level {account.get('options_trading_level')}")

    # -- chain --------------------------------------------------------------
    _rule("CHAIN")
    spot = client.spot("SPY")
    chain, parse_census = client.build_chain(DEFAULT_EXPIRIES, feed.feed, spot=spot)
    age = client.quote_age_seconds(parse_census)
    print(f"  spot                 ${spot:,.2f}")
    for key, value in parse_census.items():
        print(f"  {key:22s} {value}")
    print(f"  freshest quote age   {age:.1f}s" if age != float("inf") else "  quote age unknown")
    record["chain"] = {k: v for k, v in parse_census.items()}
    record["spot"] = spot

    for expiry in DEFAULT_EXPIRIES:
        n_c = len(chain.calls.get(expiry, {}))
        n_p = len(chain.puts.get(expiry, {}))
        print(f"  {expiry}: {n_c:4d} calls  {n_p:4d} puts")

    # -- rectangles ---------------------------------------------------------
    _rule("RECTANGLES")
    cfg = RectangleConfig(
        max_relative_spread=args.max_spread,
        violation_buffer_pct=args.buffer,
        max_coverage_ratio=args.coverage,
    )
    found, census = build_rectangles(chain, SHORT_RATE, cfg)
    for key, value in census.items():
        print(f"  {key:26s} {value:,}")
    record["rectangle_census"] = census

    episodes = dedupe_episodes(found)
    print(f"\n  detected {len(found):,} -> {len(episodes):,} after episode dedup")
    record["detected"] = len(found)
    record["episodes"] = len(episodes)

    if not episodes:
        print("\n  No violations survived the screens. That is a normal outcome.")
        if args.json:
            args.json.write_text(json.dumps(record, indent=2, default=str))
            print(f"\n  audit record -> {args.json}")
        return 0

    # -- theory gate --------------------------------------------------------
    _rule("THEORY GATE")
    by_category: dict[str, int] = {}
    gated = []
    for cand in episodes:
        result = classify(
            _to_theory_rectangle(cand),
            SPY_DIVIDENDS,
            SHORT_RATE,
            violation_size=cand.violation_size,
        )
        by_category[result.category.value] = by_category.get(result.category.value, 0) + 1
        if result.is_tradable:
            gated.append((cand, result))
    for name, count in sorted(by_category.items()):
        print(f"  {name:24s} {count:,}")
    print(f"\n  {len(gated):,} tradable after the theory gate")
    record["theory_categories"] = by_category

    # -- positions and risk -------------------------------------------------
    _rule("POSITION + RISK  (what the gates WOULD decide; nothing is sent)")
    limits = RiskLimits()
    state = AccountState(
        equity=equity,
        starting_equity=equity,
        buying_power=float(account.get("buying_power", 0) or 0),
    )
    approved, rejected = 0, {}
    examples = []

    for cand, gate in gated[:200]:
        spec = build_position(cand, PositionConfig(structure=Structure.FOUR_LEG))
        decision = evaluate(
            spec, gate.category, state, limits, datetime.now(), age,
            detected=cand, fresh=cand,
        )
        if decision.approved:
            approved += 1
            if len(examples) < 3:
                examples.append((cand, spec))
        else:
            for code, _ in decision.rejections:
                rejected[code.value] = rejected.get(code.value, 0) + 1

    print(f"  would approve            {approved:,}")
    for code, count in sorted(rejected.items(), key=lambda kv: -kv[1]):
        print(f"  reject: {code:24s} {count:,}")
    record["would_approve"] = approved
    record["reject_codes"] = rejected

    if examples:
        _rule("SAMPLE APPROVED POSITIONS")
        for cand, spec in examples:
            print(f"  {cand.T1} / {cand.T2}  K1={cand.K1:.0f} K2={cand.K2:.0f}")
            print(
                f"    violation {cand.violation_size:.4f}  severity "
                f"{cand.normalized_severity:.4f}  coverage {cand.coverage_ratio:.3f}"
            )
            print(
                f"    entry cash ${spec.entry_cash:,.2f}  max loss "
                f"${spec.max_loss_with_commissions:,.2f}"
            )

    if args.json:
        args.json.write_text(json.dumps(record, indent=2, default=str))
        print(f"\n  audit record -> {args.json}")

    _rule("REMINDER")
    print("  Nothing was sent. This scan is read-only.")
    if not feed.is_live:
        print("  Feed is indicative: treat every detection above as synthetic.")
    print("  Verify the SPY September ex-dividend date before trusting the theory gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

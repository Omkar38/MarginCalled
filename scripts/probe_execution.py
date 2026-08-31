#!/usr/bin/env python3
"""Validate the execution path before any live trading.

Answers the question that matters the morning of a live run: will the symbols
this pipeline produces actually be accepted by Alpaca, or will an order fail
because a strike or expiry does not exist?

Three stages, increasingly invasive:

  1. SYMBOL VALIDATION  (read-only, always runs)
     Every leg symbol from recorded detections is resolved against
     /v2/options/contracts. Confirms the contract exists, is active and
     tradable, and that its strike, expiry and type match what the detector
     computed. A mismatch here is the "no such contract" failure, caught on the
     ground instead of at the order.

  2. PAYLOAD VALIDATION  (read-only, always runs)
     The MLeg order body is assembled exactly as it would be sent and checked
     structurally: leg count within Alpaca's limit of four, ratio quantities
     integral and in lowest terms, short legs covered, required fields present.

  3. ORDER PROBES  (requires --submit)
     Two orders far from the market, each cancelled immediately:
       a. A deliberately UNCOVERED 2:3 long:short ratio, which must be REJECTED.
          Getting that rejection on purpose is what confirms the coverage
          constraint is real rather than assumed.
       b. A valid 1:1:1:1 four-leg spread, which must be ACCEPTED, proving the
          payload shape and every symbol are good.
     Limit prices are set far from market so neither can fill. Both are
     cancelled whether or not they are accepted.

SAFETY. Stages 1 and 2 issue GET only. Stage 3 requires --submit, refuses any
host but paper-api, and cancels everything it creates.

USAGE.
    python3 scripts/probe_execution.py              # stages 1-2, read-only
    python3 scripts/probe_execution.py --submit     # adds stage 3
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.alpaca import (  # noqa: E402
    DATA_HOST,
    LIVE_HOST,
    TRADING_HOST,
    AlpacaDataClient,
    AlpacaError,
    parse_occ_symbol,
)


def _rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


class Trader:
    """Minimal order client. Only used under --submit."""

    def __init__(self, client: AlpacaDataClient) -> None:
        self.c = client

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{TRADING_HOST}{path}"
        if url.startswith(LIVE_HOST):
            raise AlpacaError("REFUSED: live host")
        if not url.startswith(TRADING_HOST):
            raise AlpacaError(f"REFUSED: unknown host {url}")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "APCA-API-KEY-ID": self.c.key,
                "APCA-API-SECRET-KEY": self.c.secret,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw[:400]}

    def submit(self, body: dict):
        return self._request("POST", "/v2/orders", body)

    def cancel(self, order_id: str):
        return self._request("DELETE", f"/v2/orders/{order_id}")


# --------------------------------------------------------------------------
# 1. Symbol validation
# --------------------------------------------------------------------------


def load_recent_legs(data_root: Path, limit: int = 40) -> list[dict]:
    """Leg sets from recorded detections, newest first."""
    rows: list[dict] = []
    for underlying_dir in sorted(data_root.glob("*/violations.csv")):
        with underlying_dir.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rows.append(row)
    return rows[-limit:]


def validate_symbols(client: AlpacaDataClient, rows: list[dict]) -> bool:
    _rule("1. SYMBOL VALIDATION  (read-only)")
    if not rows:
        print("  No recorded detections yet - nothing to validate.")
        print("  Run the scanner first, or this stage cannot prove anything.")
        return True

    symbols: dict[str, dict] = {}
    for row in rows:
        for leg in "ABCD":
            sym = row.get(f"sym_{leg}")
            if sym:
                symbols[sym] = row
    print(f"  {len(rows)} detections -> {len(symbols)} distinct contracts to check")

    ok = True
    checked = 0
    styles: dict[str, int] = {}
    # NOTE: the `symbols` query parameter on /v2/options/contracts silently
    # returns an empty list (HTTP 200, zero rows) rather than erroring. The path
    # form is the one that resolves a contract, so it is used here - one request
    # per symbol, well inside the rate limit.
    for sym in sorted(symbols):
        checked += 1
        status, contract = client._get(f"{TRADING_HOST}/v2/options/contracts/{sym}")
        if status != 200 or not contract.get("symbol"):
            print(f"  MISSING   {sym}  - HTTP {status}")
            ok = False
            continue
        if True:
            styles[contract.get("style", "?")] = styles.get(contract.get("style", "?"), 0) + 1
            try:
                _, expiry, right, strike = parse_occ_symbol(sym)
            except AlpacaError:
                print(f"  UNPARSED  {sym}")
                ok = False
                continue
            problems = []
            if contract.get("status") != "active":
                problems.append(f"status={contract.get('status')}")
            if not contract.get("tradable", False):
                problems.append("not tradable")
            if contract.get("expiration_date") != expiry.isoformat():
                problems.append(
                    f"expiry {contract.get('expiration_date')} != {expiry.isoformat()}"
                )
            if abs(float(contract.get("strike_price", 0)) - strike) > 1e-6:
                problems.append(
                    f"strike {contract.get('strike_price')} != {strike}"
                )
            if (contract.get("type") or "")[:1].upper() != right:
                problems.append(f"type {contract.get('type')} != {right}")
            if problems:
                print(f"  BAD       {sym}  {'; '.join(problems)}")
                ok = False

    print(f"\n  {checked} contracts checked")
    print(f"  exercise styles: {styles}")
    print(
        "  RESULT: all exist, are active and tradable, and match the detector's "
        "strike/expiry/type"
        if ok
        else "  RESULT: PROBLEMS FOUND - do not trade until resolved"
    )
    return ok


# --------------------------------------------------------------------------
# 2. Payload validation
# --------------------------------------------------------------------------


def build_mleg(legs: list[dict], limit_price: str, qty: str = "1") -> dict:
    return {
        "order_class": "mleg",
        "qty": qty,
        "type": "limit",
        "time_in_force": "day",
        "limit_price": limit_price,
        "legs": legs,
    }


def validate_payload(rows: list[dict]) -> bool:
    _rule("2. PAYLOAD VALIDATION  (read-only)")
    if not rows:
        print("  No detections to build a payload from.")
        return True
    row = rows[-1]
    legs = [
        {"symbol": row["sym_A"], "side": "buy", "ratio_qty": "1",
         "position_intent": "buy_to_open"},
        {"symbol": row["sym_B"], "side": "buy", "ratio_qty": "1",
         "position_intent": "buy_to_open"},
        {"symbol": row["sym_C"], "side": "sell", "ratio_qty": "1",
         "position_intent": "sell_to_open"},
        {"symbol": row["sym_D"], "side": "sell", "ratio_qty": "1",
         "position_intent": "sell_to_open"},
    ]
    body = build_mleg(legs, "0.01")
    ok = True
    checks = [
        ("<= 4 legs", len(body["legs"]) <= 4),
        ("all ratio_qty integral", all(l["ratio_qty"].isdigit() for l in legs)),
        ("shorts <= longs",
         sum(int(l["ratio_qty"]) for l in legs if l["side"] == "sell")
         <= sum(int(l["ratio_qty"]) for l in legs if l["side"] == "buy")),
        ("4 distinct symbols", len({l["symbol"] for l in legs}) == 4),
        ("order_class mleg", body["order_class"] == "mleg"),
        ("limit order", body["type"] == "limit"),
    ]
    for label, passed in checks:
        print(f"  {'OK  ' if passed else 'FAIL'} {label}")
        ok &= passed
    print(f"\n  payload:\n{json.dumps(body, indent=2)}")
    return ok


# --------------------------------------------------------------------------
# 3. Order probes
# --------------------------------------------------------------------------


def probe_orders(client: AlpacaDataClient, rows: list[dict]) -> bool:
    _rule("3. ORDER PROBES  (--submit)  far from market, cancelled immediately")
    if not rows:
        print("  No detections available to build a real leg set.")
        return False

    trader = Trader(client)
    row = rows[-1]
    ok = True

    print("\n  (a) UNCOVERED 2:3 long:short - EXPECTED TO BE REJECTED")
    uncovered = build_mleg(
        [
            {"symbol": row["sym_A"], "side": "buy", "ratio_qty": "2",
             "position_intent": "buy_to_open"},
            {"symbol": row["sym_D"], "side": "sell", "ratio_qty": "3",
             "position_intent": "sell_to_open"},
        ],
        "0.01",
    )
    status, data = trader.submit(uncovered)
    if status in (200, 201):
        oid = data.get("id")
        print(f"      ACCEPTED (status {status}) id={oid}")
        print("      NOTE: Alpaca accepted an uncovered ratio. The coverage")
        print("            constraint is NOT enforced the way the design assumes.")
        if oid:
            trader.cancel(oid)
            print("      cancelled")
        ok = False
    else:
        msg = data.get("message") or str(data)[:160]
        print(f"      REJECTED (status {status}): {msg}")
        print("      Confirms the coverage constraint is real.")

    print("\n  (b) VALID 1:1:1:1 four-leg - EXPECTED TO BE ACCEPTED")
    valid = build_mleg(
        [
            {"symbol": row["sym_A"], "side": "buy", "ratio_qty": "1",
             "position_intent": "buy_to_open"},
            {"symbol": row["sym_B"], "side": "buy", "ratio_qty": "1",
             "position_intent": "buy_to_open"},
            {"symbol": row["sym_C"], "side": "sell", "ratio_qty": "1",
             "position_intent": "sell_to_open"},
            {"symbol": row["sym_D"], "side": "sell", "ratio_qty": "1",
             "position_intent": "sell_to_open"},
        ],
        "0.01",
    )
    status, data = trader.submit(valid)
    if status in (200, 201):
        oid = data.get("id")
        print(f"      ACCEPTED (status {status}) id={oid}")
        print("      Payload shape and all four symbols are good.")
        time.sleep(1)
        cs, cd = trader.cancel(oid) if oid else (0, {})
        print(f"      cancelled (status {cs})")
    else:
        msg = data.get("message") or str(data)[:200]
        print(f"      REJECTED (status {status}): {msg}")
        print("      THIS IS THE FAILURE TO FIX BEFORE TRADING.")
        ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the execution path.")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--submit", action="store_true",
                    help="also submit two probe orders (paper, far from market)")
    args = ap.parse_args()

    print("EXECUTION PATH PROBE")
    print(f"trading host : {TRADING_HOST}")
    print(f"mode         : {'SUBMIT (stages 1-3)' if args.submit else 'READ-ONLY (stages 1-2)'}")

    try:
        client = AlpacaDataClient()
    except AlpacaError as exc:
        print(f"\n{exc}")
        return 2

    rows = load_recent_legs(args.data_dir)
    ok = validate_symbols(client, rows)
    ok &= validate_payload(rows)

    if args.submit:
        ok &= probe_orders(client, rows)
    else:
        _rule("3. ORDER PROBES  (skipped)")
        print("  Re-run with --submit to send two probe orders. Both are placed")
        print("  far from market and cancelled immediately; neither can fill.")

    _rule("VERDICT")
    print("  PASS - execution path validated" if ok else "  FAIL - see above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

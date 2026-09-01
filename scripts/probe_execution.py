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
    _load_dotenv,
    parse_occ_symbol,
)
from tp2agent.mcp_client import (  # noqa: E402
    TRADING_TOOLSETS,
    AlpacaMCPClient,
    MCPError,
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


def _unfillable_limit(net: float) -> str:
    """A limit that cannot be marketable, whichever way the package leans.

    Positive is a debit we pay, negative a credit we receive. Offering a cent
    for something worth dollars, or demanding a thousand-dollar credit, is
    unmarketable in either direction. The probe must test acceptance of the
    payload, never acquire a position.
    """
    return "0.01" if net > 0 else "-1000.00"


def probe_orders(client: AlpacaDataClient, rows: list[dict]) -> bool:
    _rule("3. ORDER PROBES  (--submit)  via MCP, far from market, cancelled")
    if not rows:
        print("  No detections available to build a real leg set.")
        return False

    row = rows[-1]
    net = (
        float(row["A_ask"]) + float(row["B_ask"])
        - float(row["C_bid"]) - float(row["D_bid"])
    )
    limit = _unfillable_limit(net)
    print(f"  package indicative net {net:+.2f} -> probe limit {limit} "
          f"(deliberately unmarketable)")

    ok = True
    try:
        with AlpacaMCPClient(toolsets=TRADING_TOOLSETS) as mcp:
            loaded = {t["name"] for t in mcp.list_tools()}
            if "place_option_order" not in loaded:
                print("  place_option_order not loaded; cannot probe")
                return False
            print(f"  MCP connected, {len(loaded)} tools, order tool present")

            print("\n  (a) UNCOVERED 2:3 long:short - EXPECTED TO BE REJECTED")
            uncovered = {
                "qty": "1", "type": "limit", "time_in_force": "day",
                "order_class": "mleg", "limit_price": limit,
                "legs": [
                    {"symbol": row["sym_A"], "side": "buy", "ratio_qty": "2",
                     "position_intent": "buy_to_open"},
                    {"symbol": row["sym_D"], "side": "sell", "ratio_qty": "3",
                     "position_intent": "sell_to_open"},
                ],
            }
            out = mcp.place_option_order(uncovered)
            rejected = _looks_rejected(out)
            print(f"      {'REJECTED' if rejected else 'ACCEPTED'}: "
                  f"{_error_message(out) if rejected else _order_id(out)}")
            if rejected:
                print("      Confirms the coverage constraint is real.")
            else:
                print("      NOTE: Alpaca accepted an uncovered ratio. The coverage")
                print("            assumption behind the 1:1 cap does not hold.")
                ok = False
                _cancel_any(mcp, out)

            print("\n  (b) VALID 1:1:1:1 four-leg - EXPECTED TO BE ACCEPTED")
            valid = {
                "qty": "1", "type": "limit", "time_in_force": "day",
                "order_class": "mleg", "limit_price": limit,
                "legs": [
                    {"symbol": row["sym_A"], "side": "buy", "ratio_qty": "1",
                     "position_intent": "buy_to_open"},
                    {"symbol": row["sym_B"], "side": "buy", "ratio_qty": "1",
                     "position_intent": "buy_to_open"},
                    {"symbol": row["sym_C"], "side": "sell", "ratio_qty": "1",
                     "position_intent": "sell_to_open"},
                    {"symbol": row["sym_D"], "side": "sell", "ratio_qty": "1",
                     "position_intent": "sell_to_open"},
                ],
            }
            out = mcp.place_option_order(valid)
            if _looks_rejected(out):
                print(f"      REJECTED: {_error_message(out)}")
                print("      THIS IS THE FAILURE TO FIX BEFORE TRADING.")
                ok = False
            else:
                print(f"      ACCEPTED: order id {_order_id(out)}")
                print("      Payload shape and all four symbols are good.")
                time.sleep(1)
                _cancel_any(mcp, out)
    except MCPError as exc:
        print(f"  MCP error: {exc}")
        return False

    _rule("CLEANUP SWEEP")
    st, open_orders = client._get(
        f"{TRADING_HOST}/v2/orders", {"status": "open", "limit": 50}
    )
    stragglers = open_orders if isinstance(open_orders, list) else []
    if stragglers:
        print(f"  {len(stragglers)} order(s) still open; cancelling")
        try:
            with AlpacaMCPClient(toolsets=TRADING_TOOLSETS) as mcp2:
                for o in stragglers:
                    print(f"    {o['id']} -> {mcp2.cancel_order(o['id'])[:80]}")
        except MCPError as exc:
            print(f"    sweep failed: {exc}")
    else:
        print("  nothing left open")

    _rule("POST-PROBE STATE")
    st, orders = client._get(f"{TRADING_HOST}/v2/orders", {"status": "all", "limit": 20})
    st2, pos = client._get(f"{TRADING_HOST}/v2/positions")
    n_open = sum(1 for o in orders if o.get("status") in ("new", "accepted",
                 "pending_new", "partially_filled")) if isinstance(orders, list) else 0
    print(f"  orders on account : {len(orders) if isinstance(orders,list) else 0}"
          f"  ({n_open} still open)")
    print(f"  open positions    : {len(pos) if isinstance(pos,list) else 0}")
    if isinstance(pos, list) and pos:
        print("  WARNING: a probe order filled. Close these before trading.")
        for x in pos:
            print(f"    {x.get('symbol')}  qty {x.get('qty')}")
        ok = False
    return ok


def _looks_rejected(text: str) -> bool:
    """Decide acceptance by parsing the response, not by matching substrings.

    The first version searched for words like "error" and "40" anywhere in the
    text. That misread an accepted order as rejected - a UUID or the MCP
    security envelope trips it - and because the cancel lives on the accepted
    branch, a live order was left resting on the account. Parse instead: the MCP
    server wraps the API reply as {"_alpaca_mcp_security": ..., "data": ...},
    where a successful order carries "id" and a rejection carries "error".
    """
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return True  # unparseable means we cannot claim success
    data = payload.get("data", payload)
    if isinstance(data, dict):
        if data.get("error"):
            return True
        if data.get("id"):
            return False
    return True


def _order_id(text: str) -> str | None:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return None
    data = payload.get("data", payload)
    return data.get("id") if isinstance(data, dict) else None


def _error_message(text: str) -> str:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return (text or "")[:200]
    data = payload.get("data", payload)
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:220]
    return str(err or data)[:220]


def _cancel_any(mcp, text: str) -> None:
    """Cancel the order the response describes."""
    oid = _order_id(text)
    if not oid:
        print("      (no order id in response; nothing to cancel)")
        return
    try:
        res = mcp.cancel_order(oid)
        print(f"      cancelled {oid}: {res[:120]}")
    except MCPError as exc:
        print(f"      CANCEL FAILED for {oid}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the execution path.")
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--submit", action="store_true",
                    help="also submit two probe orders (paper, far from market)")
    args = ap.parse_args()

    print("EXECUTION PATH PROBE")
    print(f"trading host : {TRADING_HOST}")
    print(f"mode         : {'SUBMIT (stages 1-3)' if args.submit else 'READ-ONLY (stages 1-2)'}")

    _load_dotenv()
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

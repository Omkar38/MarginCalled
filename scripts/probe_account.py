#!/usr/bin/env python3
"""Read-only probe of the competition Alpaca paper account.

Answers, empirically, the questions that decide the build:

  1. Account      is it really $100,000, and what options level is approved?
  2. Feed         does this account get real-time OPRA, or only the indicative feed?
  3. Universe     do our target expiries actually exist and carry strikes?
  4. Quote health what fraction of the chain is usable? (the number that predicts
                  whether the scanner finds anything at all)
  5. Fields       are greeks and implied volatility present, and is open interest
                  really absent?

SAFETY. This script cannot trade. It issues HTTP GET only; there is no code path
that builds an order payload, and `_get` refuses any non-GET method and any
trading host other than paper-api. Run it as often as you like.

USAGE.
    export APCA_API_KEY_ID="PK..."
    export APCA_API_SECRET_KEY="..."
    python3 scripts/probe_account.py

Keys are read from the environment only. Nothing is written to disk, and nothing
is printed that reveals the secret.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date

TRADING_HOST = "https://paper-api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"

# Universe under consideration: both legs must expire after the 4 Sep deadline, and
# T1 must clear the September ex-dividend date by >= ~21 days for Prop 2.1(ii) to be
# satisfiable (see tests/test_theory_gate.py::test_near_leg_too_close_to_ex_date_fails).
TARGET_EXPIRIES = ["2026-10-16", "2026-11-20", "2026-12-18"]
UNDERLYING = "SPY"

TIMEOUT = 30
_CTX = ssl.create_default_context()


class ProbeError(RuntimeError):
    pass


def _load_dotenv() -> None:
    """Load .env from the project root, without overriding a real env var.

    Kept to the standard library on purpose: no python-dotenv dependency, and the
    file never leaves this machine (.env is gitignored).
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip("'\"")
            if name and name not in os.environ:
                os.environ[name] = value


def _auth() -> dict[str, str]:
    _load_dotenv()
    key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not key or not secret:
        raise ProbeError(
            "No credentials found. Either:\n"
            "  1. cp .env.example .env   and fill it in (gitignored), or\n"
            "  2. export APCA_API_KEY_ID='PK...'\n"
            "     export APCA_API_SECRET_KEY='...'"
        )
    if key.startswith("AK"):
        raise ProbeError(
            "REFUSED: that looks like a LIVE key (AK...). This project is paper-only.\n"
            "Generate paper keys with the Live/Paper toggle set to Paper; they start PK."
        )
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _get(url: str, params: dict | None = None) -> tuple[int, dict]:
    """Issue a GET. Refuses anything that is not a read against a known host."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    # Hard guard: never touch a live trading host.
    if url.startswith("https://api.alpaca.markets"):
        raise ProbeError(f"REFUSED: live trading host in {url}")
    if not (url.startswith(TRADING_HOST) or url.startswith(DATA_HOST)):
        raise ProbeError(f"REFUSED: unknown host in {url}")

    req = urllib.request.Request(url, headers=_auth(), method="GET")
    assert req.get_method() == "GET", "probe must be read-only"

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:400]}
    except urllib.error.URLError as exc:
        raise ProbeError(f"network error: {exc.reason}") from exc


def _rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# --------------------------------------------------------------------------
# 1. Account
# --------------------------------------------------------------------------


def probe_account() -> dict:
    _rule("1. ACCOUNT")
    status, data = _get(f"{TRADING_HOST}/v2/account")
    if status != 200:
        print(f"  FAILED ({status}): {data}")
        return {}

    equity = float(data.get("equity", 0) or 0)
    fields = [
        ("account (last 4)", str(data.get("account_number", "????"))[-4:]),
        ("status", data.get("status")),
        ("equity", f"${equity:,.2f}"),
        ("cash", f"${float(data.get('cash', 0) or 0):,.2f}"),
        ("buying power", f"${float(data.get('buying_power', 0) or 0):,.2f}"),
        ("options approved level", data.get("options_approved_level")),
        ("options trading level", data.get("options_trading_level")),
        ("trading blocked", data.get("trading_blocked")),
        ("account blocked", data.get("account_blocked")),
    ]
    for label, value in fields:
        print(f"  {label:26s} {value}")

    print()
    if abs(equity - 100_000) < 1:
        print("  OK    equity is exactly $100,000 as the competition requires")
    else:
        print(f"  WARN  equity is ${equity:,.2f}, not $100,000 — check the rules")

    lvl = data.get("options_trading_level")
    if lvl is not None and int(lvl) >= 3:
        print(f"  OK    options level {lvl} — multi-leg (MLeg) permitted")
    else:
        print(f"  BLOCK options level {lvl} — level 3 required for multi-leg spreads")
    return data


# --------------------------------------------------------------------------
# 2. Feed entitlement
# --------------------------------------------------------------------------


def probe_feed() -> str | None:
    _rule("2. FEED ENTITLEMENT  (the OPRA question)")
    resolved = None
    for feed in ("opra", "indicative"):
        status, data = _get(
            f"{DATA_HOST}/v1beta1/options/snapshots/{UNDERLYING}",
            {"feed": feed, "limit": 10},
        )
        n = len(data.get("snapshots", {}) or {})
        if status == 200 and n:
            print(f"  {feed:12s} OK      {n} snapshots returned")
            if resolved is None:
                resolved = feed
        else:
            msg = data.get("message") or data.get("raw") or data
            print(f"  {feed:12s} DENIED  ({status}) {str(msg)[:120]}")

    print()
    if resolved == "opra":
        print("  MODE: LIVE (OPRA).  Real consolidated NBBO. Detection is meaningful.")
    elif resolved == "indicative":
        print("  MODE: SHADOW (indicative).  Quotes are Alpaca's derivatives of OPRA,")
        print("        15-minute delayed. A TP2 determinant computed on these measures")
        print("        the derivation, not the market. Detect and log, but do not claim")
        print("        the violations are real.")
    else:
        print("  MODE: NONE. No options data at all — check entitlements before Tuesday.")
    return resolved


# --------------------------------------------------------------------------
# 3. Universe census
# --------------------------------------------------------------------------


def probe_universe() -> dict[str, int]:
    _rule("3. UNIVERSE CENSUS  (do our target expiries exist?)")
    counts: dict[str, int] = {}
    for expiry in TARGET_EXPIRIES:
        status, data = _get(
            f"{TRADING_HOST}/v2/options/contracts",
            {
                "underlying_symbols": UNDERLYING,
                "expiration_date": expiry,
                "type": "call",
                "limit": 10_000,
            },
        )
        contracts = data.get("option_contracts", []) or []
        counts[expiry] = len(contracts)
        if status != 200:
            print(f"  {expiry}  FAILED ({status}): {str(data)[:100]}")
            continue

        strikes = sorted(float(c["strike_price"]) for c in contracts) if contracts else []
        if strikes:
            n = len(strikes)
            pairs = n * (n - 1) // 2
            print(
                f"  {expiry}  {n:4d} calls  strikes {strikes[0]:.0f}–{strikes[-1]:.0f}"
                f"  -> {pairs:,} (K1<K2) pairs"
            )
        else:
            print(f"  {expiry}  no contracts returned")

    print()
    ok = [e for e, n in counts.items() if n > 0]
    if len(ok) >= 2:
        print(f"  OK    {len(ok)} usable expiries; rectangles can be formed")
    else:
        print("  BLOCK need at least two populated expiries to build a rectangle")
    return counts


# --------------------------------------------------------------------------
# 4 & 5. Quote health and available fields
# --------------------------------------------------------------------------


def probe_quote_health(feed: str, expiry: str) -> None:
    _rule(f"4. QUOTE HEALTH  ({expiry}, feed={feed})")
    status, data = _get(
        f"{DATA_HOST}/v1beta1/options/snapshots/{UNDERLYING}",
        {"feed": feed, "limit": 1000, "expiration_date": expiry},
    )
    snaps = data.get("snapshots", {}) or {}
    if status != 200 or not snaps:
        print(f"  no snapshots ({status}): {str(data)[:160]}")
        return

    total = len(snaps)
    stats = Counter()
    field_presence = Counter()
    spreads: list[float] = []

    for snap in snaps.values():
        q = snap.get("latestQuote") or {}
        bid = q.get("bp")
        ask = q.get("ap")
        bid_sz = q.get("bs")
        ask_sz = q.get("as")

        if snap.get("greeks"):
            field_presence["greeks"] += 1
        if snap.get("impliedVolatility") is not None:
            field_presence["impliedVolatility"] += 1
        if snap.get("latestTrade"):
            field_presence["latestTrade"] += 1
        for key in ("open_interest", "openInterest", "oi"):
            if snap.get(key) is not None:
                field_presence["open_interest"] += 1
                break

        if bid is None or ask is None:
            stats["no quote"] += 1
            continue
        if bid <= 0:
            stats["zero/neg bid"] += 1
            continue
        if ask <= bid:
            stats["crossed or locked"] += 1
            continue
        if not bid_sz or not ask_sz:
            stats["no displayed size"] += 1
            continue

        mid = 0.5 * (bid + ask)
        rel = (ask - bid) / mid if mid > 0 else 9.99
        spreads.append(rel)
        stats["usable"] += 1
        if rel <= 0.50:
            stats["usable & spread<=50%"] += 1

    print(f"  contracts sampled            {total}")
    for label in (
        "usable",
        "usable & spread<=50%",
        "no quote",
        "zero/neg bid",
        "crossed or locked",
        "no displayed size",
    ):
        n = stats[label]
        print(f"  {label:28s} {n:5d}  ({100 * n / total:5.1f}%)")

    if spreads:
        spreads.sort()
        med = spreads[len(spreads) // 2]
        p90 = spreads[int(0.9 * (len(spreads) - 1))]
        print(f"\n  relative spread   median {med:.1%}   p90 {p90:.1%}")

    _rule("5. FIELDS PRESENT  (feature availability)")
    for label in ("greeks", "impliedVolatility", "latestTrade", "open_interest"):
        n = field_presence[label]
        mark = "OK   " if n else "ABSENT"
        print(f"  {mark} {label:22s} {n}/{total}")
    if not field_presence["open_interest"]:
        print(
            "\n  Confirms the schema review: no open interest in the snapshot, so\n"
            "  feature_D_open_interest (F* rank 45) cannot be computed live."
        )

    usable_pct = 100 * stats["usable & spread<=50%"] / total
    print()
    if usable_pct < 10:
        print(
            f"  WARN  only {usable_pct:.1f}% of the chain is usable under a 50% spread\n"
            "        screen. The paper's joint screens left 5-7% of candidates and\n"
            "        removed aggregate profitability. Expect frequent abstention."
        )
    else:
        print(f"  {usable_pct:.1f}% of the chain is usable under a 50% spread screen")


def main() -> int:
    print("Alpaca paper-account probe — READ ONLY, no orders are placed")
    print(f"trading host : {TRADING_HOST}")
    print(f"data host    : {DATA_HOST}")
    print(f"run date     : {date.today().isoformat()}")

    try:
        _auth()
    except ProbeError as exc:
        print(f"\n{exc}")
        return 2

    try:
        probe_account()
        feed = probe_feed()
        counts = probe_universe()
        if feed:
            populated = [e for e in TARGET_EXPIRIES if counts.get(e)]
            if populated:
                probe_quote_health(feed, populated[0])
            else:
                print("\n(skipping quote health: no populated target expiry)")
        else:
            print("\n(skipping quote health: no usable feed)")
    except ProbeError as exc:
        print(f"\nPROBE ERROR: {exc}")
        return 1

    _rule("NEXT")
    print("  - record the resolved feed mode in CHECKLIST.md")
    print("  - if OPRA is denied, ask on Discord whether entitlements were granted")
    print("  - confirm the SPY September ex-dividend date and amount")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

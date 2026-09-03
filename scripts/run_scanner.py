#!/usr/bin/env python3
"""Continuous read-only scanner. Logs every scan to CSV. Places no orders.

Scans the SPY chain on a fixed interval, records the census, the margin
distribution, and any detected violations. Runs until interrupted.

Five minutes is the default interval because TP2 episodes persist about a session
(88.66% last one session in the source study), so polling faster adds quote noise
rather than signal, and repeated detections of the same rectangle collapse into
one episode anyway via the near-leg dedup.

SAFETY. Read-only throughout: the Alpaca client issues GET exclusively and refuses
live hosts, and nothing here constructs or submits an order.

USAGE.
    python3 scripts/run_scanner.py                    # every 5 min, until stopped
    python3 scripts/run_scanner.py --interval 60      # every minute
    python3 scripts/run_scanner.py --once             # a single pass
    python3 scripts/run_scanner.py --market-hours     # skip outside 09:30-16:00 ET
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import signal
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.alpaca import AlpacaDataClient, AlpacaError  # noqa: E402
from tp2agent.episodes import EpisodeTracker, episode_id  # noqa: E402
from tp2agent.rectangles import (  # noqa: E402
    RectangleConfig,
    build_rectangles,
    dedupe_episodes,
    tradability_flags,
)
from tp2agent.executor import (  # noqa: E402
    Executor,
    LimitPolicy,
    Transport,
    build_order,
)
from tp2agent.audit import AuditLog, Outcome
from tp2agent.exits import (  # noqa: E402
    ExitPolicy,
    OpenPosition,
    PositionRegistry,
    build_close_legs,
    should_exit,
)
from tp2agent.mcp_client import TRADING_TOOLSETS, AlpacaMCPClient  # noqa: E402
from tp2agent.position import build_position, config_for, structure_for  # noqa: E402
from tp2agent.risk import AccountState, RiskLimits, evaluate, revalidate  # noqa: E402
from tp2agent.store import MarginSummary, ScanStore  # noqa: E402
from tp2agent.theory_gate import (  # noqa: E402
    Contract,
    Dividend,
    Rectangle,
    classify,
    classify_european,
)

# Index options are European-style: no early-exercise feature, so the premium is
# zero by definition and Propositions 2.1-2.2 are unnecessary. Scanning these
# alongside SPY gives a clean control - the same index, one contract style needing
# the American reduction and one not.
EUROPEAN_UNDERLYINGS = {"SPX", "SPXW", "XSP", "VIX", "VIXW", "DJX", "NDX", "RUT"}

# Cash distributions apply only to the ETF; index options have none. Dividends are
# fetched live from Alpaca's corporate-actions endpoint rather than hardcoded, and
# a horizon is computed beyond which the absence of an undeclared distribution
# cannot be asserted.
SHORT_RATE = 0.045

_stop = False


def _handle_signal(signum, frame):  # noqa: ANN001, ARG001
    global _stop
    _stop = True
    print("\n  stopping after this scan...")


def _to_theory_rectangle(cand) -> Rectangle:
    return Rectangle(
        signal_date=cand.signal_date,
        A=Contract("A", cand.A.strike, cand.A.expiry, cand.A.quote.bid, cand.A.quote.ask),
        B=Contract("B", cand.B.strike, cand.B.expiry, cand.B.quote.bid, cand.B.quote.ask),
        C=Contract("C", cand.C.strike, cand.C.expiry, cand.C.quote.bid, cand.C.quote.ask),
        D=Contract("D", cand.D.strike, cand.D.expiry, cand.D.quote.bid, cand.D.quote.ask),
    )


def _reprice(cand, client, feed: str):
    """Re-price the four legs from a snapshot taken now.

    Returns a candidate rebuilt on current quotes, or None if it can no longer
    be priced at all. Passing the ORIGINAL candidate as `fresh` - which is what
    this code did until now - made the revalidation gate vacuous: it compared
    the rectangle against itself, so violation_retained was exactly 1.0 on every
    record by construction and the gate could never fire. The scan-wide snapshot
    is minutes old by the time an order is built, which is exactly the window
    the gate exists to cover.
    """
    from dataclasses import replace

    legs = {"A": cand.A, "B": cand.B, "C": cand.C, "D": cand.D}
    try:
        snaps = client.snapshots_for_symbols([l.symbol for l in legs.values()], feed)
    except Exception:  # noqa: BLE001
        return None                      # cannot confirm -> treat as gone

    fresh = {}
    for name, leg in legs.items():
        snap = snaps.get(leg.symbol)
        if not snap:
            return None
        q = snap.get("latestQuote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        if bid is None or ask is None:
            return None
        quote = replace(
            leg.quote, bid=float(bid), ask=float(ask),
            bid_size=float(q.get("bs") or 0), ask_size=float(q.get("as") or 0),
        )
        fresh[name] = replace(leg, quote=quote)

    lhs = fresh["A"].quote.ask * fresh["B"].quote.ask
    rhs = fresh["C"].quote.bid * fresh["D"].quote.bid
    return replace(
        cand, A=fresh["A"], B=fresh["B"], C=fresh["C"], D=fresh["D"],
        lhs=lhs, rhs=rhs, violation_size=rhs - lhs,
    )


def _determinant(cand) -> dict:
    """The inequality as it stood at the moment of the decision."""
    # normalized_severity divides by (lhs + rhs); the tick bound is a relative
    # bound on the PRODUCT and is compared against violation/rhs. Those differ
    # by a factor of about two, so storing them adjacent invites the reading
    # that a trade was approved below its own tick bound. The comparable
    # quantity and the test's own verdict are recorded alongside so the check
    # is self-evident rather than reconstructible.
    required = cand.rhs * cand.tick_bound
    return {
        "lhs": cand.lhs,
        "rhs": cand.rhs,
        "violation_size": cand.violation_size,
        "normalized_severity": cand.normalized_severity,
        "severity_over_rhs": cand.severity_over_rhs,
        "tick_bound": cand.tick_bound,
        "tick_bound_required": required,
        "clears_tick_bound": cand.violation_size > required,
        "K1": cand.K1, "K2": cand.K2,
        "K1_adj": cand.K1_adj, "K2_adj": cand.K2_adj,
        "T1": cand.T1.isoformat(), "T2": cand.T2.isoformat(),
    }


def _leg_quotes(cand) -> dict:
    """Per-leg quotes as seen. Stored so a decision can be re-derived later
    without trusting that the feed still says the same thing."""
    return {
        name: {"symbol": leg.symbol, "strike": leg.strike,
               "expiry": leg.expiry.isoformat(),
               "bid": leg.quote.bid, "ask": leg.quote.ask}
        for name, leg in (("A", cand.A), ("B", cand.B), ("C", cand.C), ("D", cand.D))
    }


class TradeContext:
    """Turns approved candidates into orders. Off unless --trade is passed.

    The structure is chosen by the underlying, not by preference: Alpaca rejects
    a multi-leg order whose European legs span different expirations (422 /
    42210000), so index underlyings can only trade the same-expiry T1 pair while
    American ones may use the full four-leg rectangle. Both verified live.
    """

    def __init__(self, client, underlying: str, enabled: bool, dry_run: bool,
                 max_orders: int, shade: float, data_dir: Path,
                 limits: "RiskLimits | None" = None) -> None:
        self.client = client
        self.underlying = underlying
        self.enabled = enabled
        self.dry_run = dry_run
        self.max_orders = max_orders
        self.policy = LimitPolicy(shade_spreads=shade)
        self.limits = limits or RiskLimits()
        self.feed = "indicative"     # set by the scan loop once the feed resolves
        self.structure = structure_for(underlying)
        self.sent = 0
        self.mcp = None
        self.executor = None
        self.registry = PositionRegistry(Path(data_dir) / "positions.jsonl")
        self.pending: dict[str, dict] = {}
        self.exit_policy = ExitPolicy()
        # Must follow --data-dir, not a hardcoded path, or a test run writes
        # its orders into the live data directory.
        self.log = Path(data_dir) / "orders.jsonl"
        # The decision log is separate from orders.jsonl on purpose: that file
        # records what was SENT, which is a survivorship-biased view of the
        # agent's behaviour. Most candidates are refused and never appear there.
        self.audit = AuditLog(Path(data_dir) / "decisions.jsonl")

    def open(self) -> None:
        if not self.enabled:
            return
        self.mcp = AlpacaMCPClient(toolsets=TRADING_TOOLSETS)
        self.mcp.start()
        self.executor = Executor(
            self.client, transport=Transport.MCP, mcp=self.mcp
        )

    def close(self) -> None:
        if self.mcp is not None:
            self.mcp.stop()
            self.mcp = None

    def reconcile(self, ts) -> None:
        """Turn accepted orders into recorded positions.

        An accepted order is not a position. Until it fills we owe nothing, and
        treating acceptance as ownership would have the exit logic trying to
        close something that does not exist.
        """
        if self.executor is None:
            return
        for pos in list(self.registry.positions.values()):
            if not pos.is_open or pos.close_order_id:
                continue
        # Any order we sent that has since filled becomes a position.
        for episode_id, pending in list(self.pending.items()):
            st, order = self.executor.order(pending["order_id"])
            if st != 200:
                continue
            status = order.get("status")
            if status == "filled":
                legs = pending["spec"].legs
                long_leg = next(l for l in legs if l.side.value == "buy")
                short_leg = next(l for l in legs if l.side.value == "sell")
                self.registry.add(OpenPosition(
                    episode_id=episode_id,
                    underlying=self.underlying,
                    denomination=self.structure.name,
                    order_id=pending["order_id"],
                    opened_at=ts,
                    long_symbol=long_leg.symbol,
                    short_symbol=short_leg.symbol,
                    long_expiry=date.fromisoformat(long_leg.expiry),
                    short_expiry=date.fromisoformat(short_leg.expiry),
                    entry_long_price=long_leg.entry_price,
                    entry_short_price=short_leg.entry_price,
                ))
                print(f"      FILLED {episode_id} -> position recorded")
                del self.pending[episode_id]
            elif status in ("canceled", "expired", "rejected"):
                del self.pending[episode_id]

    def manage_exits(self, ts, tracker) -> None:
        """Close positions whose thesis resolved, timed out, or hit the deadline."""
        if self.executor is None:
            return
        for pos in self.registry.open_positions():
            ep = tracker.episodes.get(pos.episode_id) if tracker else None
            decision = should_exit(pos, ep.status if ep else None, ts,
                                   self.exit_policy)
            if not decision.should_close:
                continue
            legs = build_close_legs(pos)
            shade = (self.exit_policy.deadline_shade_spreads if decision.urgent
                     else self.exit_policy.shade_spreads)
            body = {
                "qty": str(pos.qty), "type": "limit", "time_in_force": "day",
                "order_class": "mleg",
                # Closing a debit spread is a credit to us; shade against the
                # indicative quote unless the deadline makes price secondary.
                "limit_price": f"{-(pos.entry_long_price - pos.entry_short_price) + shade:.2f}",
                "legs": legs,
            }
            try:
                out = self.mcp.place_option_order(body) if not self.dry_run else "dry-run"
                oid = None
                if not self.dry_run:
                    try:
                        oid = json.loads(out).get("data", {}).get("id")
                    except Exception:  # noqa: BLE001
                        oid = None
                self.registry.close(pos.episode_id, oid, decision.reason.value, ts)
                print(f"      EXIT {'(dry-run) ' if self.dry_run else ''}"
                      f"{pos.episode_id} — {decision.reason.value}: {decision.detail[:70]}")
                self._write({"ts": ts.isoformat(timespec="seconds"),
                             "event": "exit", "position": pos.to_record(),
                             "decision": decision.to_record()})
            except Exception as exc:  # noqa: BLE001
                print(f"      EXIT FAILED {pos.episode_id}: {exc}")

    def consider(self, ts, underlying, gated, quote_age, spot) -> None:
        if not self.enabled or self.executor is None:
            return
        if self.sent >= self.max_orders:
            return

        acct = self.client.account()
        equity = float(acct.get("equity", 0) or 0)
        held = {p.get("symbol") for p in (self.executor.positions() or [])}
        held |= self.registry.held_symbols()
        state = AccountState(
            equity=equity,
            starting_equity=equity,
            buying_power=float(acct.get("buying_power", 0) or 0),
            open_position_count=len(held),
            open_leg_symbols=frozenset(held),
        )
        limits = self.limits

        for cand, category in gated:
            if self.sent >= self.max_orders:
                return
            spec = build_position(cand, config_for(underlying))
            if not spec.is_executable:
                self.audit.log(
                    underlying=underlying, episode_key=cand.episode_key,
                    outcome=Outcome.NOT_EXECUTABLE, stage="position",
                    theory_category=category.value,
                    reason=spec.rejected_reason or "no covered whole-contract ratio",
                    determinant=_determinant(cand), quotes=_leg_quotes(cand),
                )
                continue
            # Two passes, because re-pricing costs an API call per candidate.
            #
            # The first pass runs every gate that needs no network, using the
            # scan's own quotes so revalidation is trivially satisfied. Only a
            # candidate that clears all of those is worth re-pricing, and there
            # are a handful of those per scan rather than hundreds.
            #
            # Doing it the other way round - re-pricing every candidate up front
            # - issued 718 snapshot requests in one SPX scan, was rate limited,
            # and the failures came back as VIOLATION_GONE. That reads as "the
            # edge evaporated" when it actually means "we never asked". 161 of
            # 369 gone-refusals in the first live cycle were this, not a real
            # signal.
            decision = evaluate(
                spec, category, state, limits, datetime.now(), quote_age,
                detected=cand, fresh=cand,
            )
            if decision.approved:
                fresh = _reprice(cand, self.client, self.feed)
                revalidate(cand, fresh, limits, decision)
                decision.approved = not decision.rejections
            else:
                # Pass 1 satisfies revalidation trivially by comparing the
                # rectangle with itself, so a candidate rejected here records
                # "100% of the violation retained" for a revalidation that
                # never ran. The narrator read exactly that off a live log.
                # Clear it rather than leave a check reported as passed.
                decision.violation_retained = None
                decision.fresh_violation_size = None
                decision.checks_passed = [
                    c for c in decision.checks_passed
                    if "violation" not in c.lower()
                ]
                decision.checks_passed.append(
                    "revalidation not run (rejected before re-pricing)"
                )
            record = {
                "ts": ts.isoformat(timespec="seconds"),
                "underlying": underlying,
                "structure": self.structure.value,
                "episode": cand.episode_key,
                "category": category.value,
                "risk": decision.to_record(),
                "position": spec.to_record(),
            }
            if not decision.approved:
                self._write(record)
                self.audit.log(
                    underlying=underlying, episode_key=cand.episode_key,
                    outcome=Outcome.RISK_REJECTED, stage="risk",
                    theory_category=category.value,
                    denomination=self.structure.name,
                    reason="; ".join(
                        f"{c.value}: {m}" for c, m in decision.rejections
                    ) or "risk gates refused",
                    determinant=_determinant(cand), quotes=_leg_quotes(cand),
                    risk=decision.to_record(),
                )
                continue
            plan = build_order(spec, self.policy)
            record["order"] = plan.to_record()
            try:
                result = self.executor.submit(plan, decision, dry_run=self.dry_run)
                record["result"] = result
                self.sent += 1
                oid = None
                if not self.dry_run:
                    try:
                        oid = json.loads(result.get("response", "{}")).get("data", {}).get("id")
                    except Exception:  # noqa: BLE001
                        oid = None
                if oid:
                    self.pending[cand.episode_key] = {"order_id": oid, "spec": spec}
                self.audit.log(
                    underlying=underlying, episode_key=cand.episode_key,
                    outcome=Outcome.TRADED, stage="execution",
                    theory_category=category.value,
                    denomination=self.structure.name,
                    reason=("dry run - not sent to the broker" if self.dry_run
                            else "submitted via MCP"),
                    determinant=_determinant(cand), quotes=_leg_quotes(cand),
                    risk=decision.to_record(), order=plan.to_record(),
                    broker={"order_id": oid, "status": "dry_run" if self.dry_run else "submitted"},
                )
                print(f"      ORDER {'(dry-run) ' if self.dry_run else ''}"
                      f"{self.structure.value} {cand.episode_key} "
                      f"limit {plan.limit_price:+.2f}")
            except Exception as exc:  # noqa: BLE001
                record["result"] = {"error": f"{type(exc).__name__}: {exc}"}
                self.audit.log(
                    underlying=underlying, episode_key=cand.episode_key,
                    outcome=Outcome.ORDER_FAILED, stage="execution",
                    theory_category=category.value,
                    denomination=self.structure.name,
                    reason=f"{type(exc).__name__}: {exc}",
                    determinant=_determinant(cand), quotes=_leg_quotes(cand),
                    risk=decision.to_record(), order=plan.to_record(),
                )
                print(f"      ORDER FAILED: {exc}")
            self._write(record)

    def _write(self, record: dict) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")


def _in_market_hours(now: datetime) -> bool:
    """Rough US equity session check in local time. Weekends excluded."""
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def scan_once(
    client: AlpacaDataClient,
    store: ScanStore,
    cfg: RectangleConfig,
    underlying: str,
    expiries: list[date],
    tracker: EpisodeTracker | None = None,
    dividends: list[Dividend] | None = None,
    certain_through: date | None = None,
    trader: "TradeContext | None" = None,
) -> dict:
    started = time.monotonic()
    ts = datetime.now()
    is_european = underlying.upper() in EUROPEAN_UNDERLYINGS

    feed = client.resolve_feed(underlying)
    spot, spot_src = client.resolve_spot(underlying, expiries[0], feed.feed, SHORT_RATE)
    chain, parse_census = client.build_chain(
        expiries, feed.feed, underlying=underlying, spot=spot
    )
    age = client.quote_age_seconds(parse_census)

    margins: list[float] = []
    found, census = build_rectangles(chain, SHORT_RATE, cfg, margins=margins)
    episodes = dedupe_episodes(found)
    summary = MarginSummary.from_margins(margins)
    duration = time.monotonic() - started

    store.record_scan(
        ts, feed.feed, spot, age, duration, census, len(episodes), summary
    )

    categories: dict[str, str] = {}
    gated: list = []
    n_tradable = 0
    for cand in episodes:
        rect = _to_theory_rectangle(cand)
        gate = (
            classify_european(rect)
            if is_european
            else classify(
                rect,
                dividends or [],
                SHORT_RATE,
                violation_size=cand.violation_size,
                certain_through=certain_through,
            )
        )
        categories[episode_id(underlying, cand)] = gate.category.value
        flags = tradability_flags(cand)
        if flags.tradable:
            n_tradable += 1
            if gate.is_tradable:
                gated.append((cand, gate.category))
            elif trader is not None and trader.enabled:
                # Executable and liquid, refused purely on theory. This is the
                # most informative refusal the agent makes, so it is recorded
                # even though it never reaches the order path.
                trader.audit.log(
                    underlying=underlying, episode_key=cand.episode_key,
                    outcome=Outcome.THEORY_BLOCKED, stage="theory_gate",
                    theory_category=gate.category.value,
                    reason="; ".join(gate.reasons) or "the gate could not certify no early exercise",
                    determinant=_determinant(cand), quotes=_leg_quotes(cand),
                )
        elif trader is not None and trader.enabled:
            # Detected, but not executable on the terms quoted. Recorded so the
            # log shows the whole funnel: without this the most common outcome -
            # a real violation we cannot trade - leaves no trace at all, and a
            # scan that found 12 violations and sent 0 orders looks unexplained.
            trader.audit.log(
                underlying=underlying, episode_key=cand.episode_key,
                outcome=Outcome.NOT_EXECUTABLE, stage="tradability",
                theory_category=gate.category.value,
                reason="; ".join(flags.reasons) or "failed the execution screen",
                determinant=_determinant(cand), quotes=_leg_quotes(cand),
                extra={"coverage_ratio": flags.coverage_ratio,
                       "min_leg_mid": flags.min_leg_mid},
            )
        store.record_violation(
            ts, feed.feed, spot, cand, gate.category.value, gate.is_tradable, flags
        )

    ep_stats = {}
    if tracker is not None:
        ep_stats = tracker.observe(ts, chain, episodes, categories)

    if trader is not None and trader.enabled:
        trader.feed = feed.feed
        trader.reconcile(ts)
        trader.manage_exits(ts, tracker)
        if gated:
            trader.consider(ts, underlying, gated, age, spot)

    closest = summary.maximum if summary.count else float("nan")
    print(
        f"  {ts.strftime('%H:%M:%S')}  {underlying:4s} ${spot:9,.2f}  "
        f"considered {census['rectangles_considered']:6,}  "
        f"measured {summary.count:5,}  "
        f"closest {closest:+.4f}  "
        f"detected {census['detected']:3,}  "
        f"[{duration:.1f}s]"
    )
    if episodes:
        print(
            f"      *** {len(episodes)} VIOLATION(S) recorded "
            f"({n_tradable} executable) ***"
        )
    if ep_stats and any(ep_stats.values()):
        s = tracker.summary() if tracker else {}
        print(
            f"      episodes: +{ep_stats['new']} new, {ep_stats['continuing']} continuing, "
            f"{ep_stats['reverted']} reverted, {ep_stats['unobservable']} unobservable "
            f"| tracking {s.get('active', 0)} active / {s.get('total', 0)} total"
        )
    return census


def main() -> int:
    ap = argparse.ArgumentParser(description="Continuous read-only TP2 scanner.")
    ap.add_argument("--underlying", default="SPY", help="SPY, SPX, XSP, ...")
    ap.add_argument("--min-dte", type=int, default=30)
    ap.add_argument("--max-dte", type=int, default=150)
    ap.add_argument("--max-expiries", type=int, default=3)
    ap.add_argument("--interval", type=int, default=300, help="seconds (default 300)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--market-hours", action="store_true", help="skip outside RTH")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="default: data/<UNDERLYING>")
    # Detection screens only. Execution-side limits (coverage ratio, leg price)
    # belong in tradability_flags, downstream - putting them here suppresses the
    # measurement itself. These defaults previously re-imposed the execution
    # screens and cut detections from ~426 to 1-3 per scan.
    ap.add_argument("--max-spread", type=float, default=0.50,
                    help="liquidity screen: max relative spread per leg")
    ap.add_argument("--buffer", type=float, default=0.0,
                    help="extra margin above the tick bound; 0 = tick bound only")
    ap.add_argument("--coverage", type=float, default=1e9,
                    help="DETECTION coverage cap; effectively off by default. "
                         "The execution cap lives in tradability_flags.")
    ap.add_argument("--trade", action="store_true",
                    help="submit orders for approved candidates (off by default)")
    ap.add_argument("--live-orders", action="store_true",
                    help="with --trade, actually send instead of dry-run")
    ap.add_argument("--max-orders", type=int, default=3,
                    help="hard cap on orders per scanner run")
    ap.add_argument("--max-loss-pct", type=float, default=None,
                    help="per-trade max loss cap as a fraction of equity "
                         "(default: RiskLimits, currently 0.025)")
    ap.add_argument("--max-aggregate-pct", type=float, default=None,
                    help="aggregate max loss cap as a fraction of equity "
                         "(default: RiskLimits, currently 0.10)")
    ap.add_argument("--shade", type=float, default=1.0,
                    help="limit shading in package spreads")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    underlying = args.underlying.upper()
    data_dir = args.data_dir or Path("data") / underlying
    is_european = underlying in EUROPEAN_UNDERLYINGS

    if args.trade and args.live_orders:
        banner = "TP2 SCANNER — LIVE ORDERS ENABLED"
    elif args.trade:
        banner = "TP2 SCANNER — trading path active, dry-run (nothing is sent)"
    else:
        banner = "TP2 SCANNER — READ ONLY, no orders are placed"
    print(banner)
    print(f"underlying : {underlying}"
          f"{'  (European — Props 2.1-2.2 not applicable)' if is_european else '  (American)'}")
    print(f"interval   : {args.interval}s")
    print(f"data dir   : {data_dir.resolve()}")

    try:
        client = AlpacaDataClient()
    except AlpacaError as exc:
        print(f"\n{exc}")
        return 2

    try:
        feed = client.resolve_feed(underlying)
        expiries = client.discover_quoted_expiries(
            underlying, feed.feed, args.min_dte, args.max_dte
        )[: args.max_expiries]
    except AlpacaError as exc:
        print(f"\n{exc}")
        return 1
    if len(expiries) < 2:
        print(f"\n  need >=2 quoted expiries, found {len(expiries)}")
        return 1
    print(f"expiries   : {', '.join(e.isoformat() for e in expiries)}")

    dividends: list[Dividend] = []
    certain_through = None
    if not is_european:
        try:
            certain_through, raw = client.dividend_horizon(underlying, date.today())
            dividends = [Dividend(ex, rate) for ex, rate, _ in raw]
            print(f"dividends  : {len(dividends)} announced; "
                  f"absence assertable through {certain_through.isoformat()}")
        except AlpacaError as exc:
            print(f"dividends  : lookup failed ({exc}); rectangles spanning an "
                  f"undeclared distribution will be left unresolved")

    store = ScanStore(data_dir)
    tracker = EpisodeTracker(data_dir, underlying)
    limits = RiskLimits()
    if args.max_loss_pct is not None:
        limits = replace(limits, max_loss_per_trade_pct=args.max_loss_pct)
    if args.max_aggregate_pct is not None:
        limits = replace(limits, max_aggregate_loss_pct=args.max_aggregate_pct)
    trader = TradeContext(
        client, underlying, args.trade, not args.live_orders,
        args.max_orders, args.shade, data_dir, limits,
    )
    trader.open()
    print(f"trading    : {'ON' if args.trade else 'off'}"
          f"{'' if not args.trade else (' (dry-run)' if trader.dry_run else ' LIVE')}"
          f"  structure {trader.structure.value}  cap {args.max_orders}")
    if args.trade:
        print(f"risk       : per-trade {limits.max_loss_per_trade_pct:.2%} "
              f"aggregate {limits.max_aggregate_loss_pct:.2%}  shade {args.shade}")
    cfg = RectangleConfig(
        max_relative_spread=args.max_spread,
        violation_buffer_pct=args.buffer,
        max_coverage_ratio=args.coverage,
    )

    # Unbuffered stdout. When output is redirected to a file Python block-buffers
    # it, so a stalled scanner writes nothing and looks identical to a healthy one
    # that has not printed yet. Losing a session to that is not worth the buffer.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    scans = 0
    heartbeats = 0
    while not _stop:
        now = datetime.now()
        if args.market_hours and not _in_market_hours(now):
            heartbeats += 1
            # Only every twelfth idle tick (~1h at the default interval) so an
            # overnight log stays readable, but silence still means "stalled".
            if heartbeats % 12 == 1:
                print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}  outside market hours, "
                      f"sleeping (idle tick {heartbeats})")
        else:
            try:
                scan_once(
                    client, store, cfg, underlying, expiries, tracker,
                    dividends, certain_through, trader,
                )
                scans += 1
            except AlpacaError as exc:
                print(f"  {now.strftime('%H:%M:%S')}  scan failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {now.strftime('%H:%M:%S')}  unexpected: {type(exc).__name__}: {exc}")

        if args.once or _stop:
            break
        for _ in range(args.interval):
            if _stop:
                break
            time.sleep(1)

    trader.close()
    summary = tracker.summary()
    print(f"\n  {scans} scan(s) recorded to {store.scans}")
    print(
        f"  episodes: {summary['total']} total, {summary['active']} active, "
        f"{summary['reverted']} reverted"
    )
    if summary["reverted"]:
        print(
            f"  reverted-episode duration: median {summary['median_duration_s']:.0f}s, "
            f"max {summary['max_duration_s']:.0f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

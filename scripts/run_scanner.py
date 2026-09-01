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
from tp2agent.mcp_client import TRADING_TOOLSETS, AlpacaMCPClient  # noqa: E402
from tp2agent.position import build_position, config_for, structure_for  # noqa: E402
from tp2agent.risk import AccountState, RiskLimits, evaluate  # noqa: E402
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


class TradeContext:
    """Turns approved candidates into orders. Off unless --trade is passed.

    The structure is chosen by the underlying, not by preference: Alpaca rejects
    a multi-leg order whose European legs span different expirations (422 /
    42210000), so index underlyings can only trade the same-expiry T1 pair while
    American ones may use the full four-leg rectangle. Both verified live.
    """

    def __init__(self, client, underlying: str, enabled: bool, dry_run: bool,
                 max_orders: int, shade: float, data_dir: Path) -> None:
        self.client = client
        self.underlying = underlying
        self.enabled = enabled
        self.dry_run = dry_run
        self.max_orders = max_orders
        self.policy = LimitPolicy(shade_spreads=shade)
        self.structure = structure_for(underlying)
        self.sent = 0
        self.mcp = None
        self.executor = None
        # Must follow --data-dir, not a hardcoded path, or a test run writes
        # its orders into the live data directory.
        self.log = Path(data_dir) / "orders.jsonl"

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

    def consider(self, ts, underlying, gated, quote_age, spot) -> None:
        if not self.enabled or self.executor is None:
            return
        if self.sent >= self.max_orders:
            return

        acct = self.client.account()
        equity = float(acct.get("equity", 0) or 0)
        held = {p.get("symbol") for p in (self.executor.positions() or [])}
        state = AccountState(
            equity=equity,
            starting_equity=equity,
            buying_power=float(acct.get("buying_power", 0) or 0),
            open_position_count=len(held),
            open_leg_symbols=frozenset(held),
        )
        limits = RiskLimits()

        for cand, category in gated:
            if self.sent >= self.max_orders:
                return
            spec = build_position(cand, config_for(underlying))
            if not spec.is_executable:
                continue
            decision = evaluate(
                spec, category, state, limits, datetime.now(), quote_age,
                detected=cand, fresh=cand,
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
                continue
            plan = build_order(spec, self.policy)
            record["order"] = plan.to_record()
            try:
                result = self.executor.submit(plan, decision, dry_run=self.dry_run)
                record["result"] = result
                self.sent += 1
                print(f"      ORDER {'(dry-run) ' if self.dry_run else ''}"
                      f"{self.structure.value} {cand.episode_key} "
                      f"limit {plan.limit_price:+.2f}")
            except Exception as exc:  # noqa: BLE001
                record["result"] = {"error": f"{type(exc).__name__}: {exc}"}
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
        store.record_violation(
            ts, feed.feed, spot, cand, gate.category.value, gate.is_tradable, flags
        )

    ep_stats = {}
    if tracker is not None:
        ep_stats = tracker.observe(ts, chain, episodes, categories)

    if trader is not None and gated:
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
    trader = TradeContext(
        client, underlying, args.trade, not args.live_orders,
        args.max_orders, args.shade, data_dir,
    )
    trader.open()
    print(f"trading    : {'ON' if args.trade else 'off'}"
          f"{'' if not args.trade else (' (dry-run)' if trader.dry_run else ' LIVE')}"
          f"  structure {trader.structure.value}  cap {args.max_orders}")
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

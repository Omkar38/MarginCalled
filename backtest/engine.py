"""Backtest of the T1 and K2 denominations on captured violations.

TP2 requires C(K1,T1) C(K2,T2) >= C(K1~,T2) C(K2~,T1). A violation reverses it,
so the left-hand pair is undervalued relative to the right-hand pair: the trade
buys the left side and sells the right.

With A = (K1,T1), B = (K2,T2), C = (K1~,T2), D = (K2~,T1), Table 5.1 of
Glasserman, Li & Pirjol gives the two denominations worth trading:

    T1   buy A, sell D      both legs expire at T1 - a vertical
    K2   buy B, sell D      legs at T2 and T1     - a diagonal

Both short D, the near-dated leg. The denomination names the parameter the two
traded contracts share.

WEIGHTS. Each traded leg is weighted by the price of the leg it does not trade,
which makes the entry premium equal the violation size. Measured on executable
quote sides:

    T1:  qty_long = B_ask, qty_short = C_bid
         entry cash = C_bid*D_bid - B_ask*A_ask = rhs - lhs = V

    K2:  qty_long = A_ask, qty_short = C_bid
         entry cash = C_bid*D_bid - A_ask*B_ask = rhs - lhs = V

The paper states this identity for mid prices; using quote sides preserves it
exactly while keeping every price one a trade could actually cross.

SIZING. Two modes:
    RATIO   the theoretical weights above. Not executable - price(C) > price(B)
            on every rectangle, so the short leg outnumbers the long and a
            multi-leg order with uncovered shorts is rejected.
    CAPPED  1:1, which is what can actually be sent. This destroys the
            premium-equals-violation property, and that is reported rather than
            hidden.

EXITS. Three modes:
    REVERSION   close at the first observation where the violation is gone.
                Uses per-leg quotes from episode_path.csv: sell the long at bid,
                buy back the short at ask.
    EXPIRATION  hold to expiry and settle at intrinsic value. Only meaningful
                when the expiry has actually occurred; a terminal underlying
                price must be supplied.
    TIME_STOP   close at the last observation available.

Costs default to zero: Alpaca is commission-free on US options and paper
trading simulates no fees, which is what the competition scores.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path

__all__ = [
    "Denomination",
    "Sizing",
    "ExitMode",
    "Trade",
    "BacktestConfig",
    "load_violations",
    "load_paths",
    "simulate",
    "run",
]

CONTRACT_MULTIPLIER = 100.0
COMPETITION_END = date(2026, 9, 4)


class Denomination(str, Enum):
    T1 = "T1"
    K2 = "K2"


class Sizing(str, Enum):
    RATIO = "ratio"      # the paper's price weights; not executable
    CAPPED = "capped"    # 1:1, what Alpaca will accept


class ExitMode(str, Enum):
    REVERSION = "reversion"
    EXPIRATION = "expiration"
    TIME_STOP = "time_stop"


@dataclass(frozen=True)
class BacktestConfig:
    sizing: Sizing = Sizing.CAPPED
    exit_mode: ExitMode = ExitMode.REVERSION
    cost_per_contract_side: float = 0.0
    competition_end: date = COMPETITION_END
    # When the near leg expires on or before the competition end, the position
    # can be held to expiry and settled, which is the strategy as written.
    # Otherwise only a traded exit is available.
    prefer_expiry_when_possible: bool = True


@dataclass
class Trade:
    episode_id: str
    underlying: str
    denomination: Denomination
    sizing: Sizing
    exit_mode: ExitMode

    entry_ts: str = ""
    exit_ts: str = ""
    long_symbol: str = ""
    short_symbol: str = ""
    T1: date | None = None
    T2: date | None = None

    qty_long: float = 0.0
    qty_short: float = 0.0
    entry_long_price: float = 0.0   # ask - we buy
    entry_short_price: float = 0.0  # bid - we sell
    exit_long_price: float = 0.0    # bid - we sell to close
    exit_short_price: float = 0.0   # ask - we buy to close

    entry_cash: float = 0.0
    exit_cash: float = 0.0
    costs: float = 0.0
    violation_size: float = 0.0
    settled_at_expiry: bool = False
    resolved: bool = False
    executable: bool = True   # False for fractional (RATIO) sizing
    note: str = ""

    @property
    def gross_pnl(self) -> float:
        return (self.entry_cash + self.exit_cash) * CONTRACT_MULTIPLIER

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.costs

    @property
    def holding_seconds(self) -> float:
        if not self.entry_ts or not self.exit_ts:
            return 0.0
        return (
            datetime.fromisoformat(self.exit_ts) - datetime.fromisoformat(self.entry_ts)
        ).total_seconds()

    def to_row(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "underlying": self.underlying,
            "denomination": self.denomination.value,
            "sizing": self.sizing.value,
            "exit_mode": self.exit_mode.value,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "long_symbol": self.long_symbol,
            "short_symbol": self.short_symbol,
            "T1": self.T1.isoformat() if self.T1 else "",
            "T2": self.T2.isoformat() if self.T2 else "",
            "qty_long": f"{self.qty_long:.6f}",
            "qty_short": f"{self.qty_short:.6f}",
            "entry_long_price": f"{self.entry_long_price:.4f}",
            "entry_short_price": f"{self.entry_short_price:.4f}",
            "exit_long_price": f"{self.exit_long_price:.4f}",
            "exit_short_price": f"{self.exit_short_price:.4f}",
            "entry_cash": f"{self.entry_cash:.6f}",
            "exit_cash": f"{self.exit_cash:.6f}",
            "violation_size": f"{self.violation_size:.6f}",
            "gross_pnl": f"{self.gross_pnl:.2f}",
            "costs": f"{self.costs:.2f}",
            "net_pnl": f"{self.net_pnl:.2f}",
            "holding_seconds": f"{self.holding_seconds:.0f}",
            "settled_at_expiry": int(self.settled_at_expiry),
            "executable": int(self.executable),
            "resolved": int(self.resolved),
            "note": self.note,
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_violations(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_paths(path: Path) -> dict[str, list[dict]]:
    """Observation paths grouped by episode, in time order."""
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.setdefault(row["episode_id"], []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: (int(r.get("event_index", 0)), r.get("ts", "")))
    return out


def _episode_id_for(underlying: str, row: dict) -> str:
    """Recompute the episode key from the violation's four symbols."""
    import hashlib

    raw = "|".join(
        [underlying, row["sym_A"], row["sym_B"], row["sym_C"], row["sym_D"]]
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


def _legs_for(denom: Denomination) -> tuple[str, str, str]:
    """(long_leg, short_leg, weight_source_for_long).

    The long leg is weighted by the price of the leg it does not trade; the
    short leg is always weighted by C.
    """
    if denom is Denomination.T1:
        return "A", "D", "B"
    return "B", "D", "A"


def simulate(
    violation: dict,
    path: list[dict],
    denom: Denomination,
    cfg: BacktestConfig,
    underlying: str,
    terminal_price: float | None = None,
) -> Trade:
    long_leg, short_leg, weight_leg = _legs_for(denom)
    t1 = date.fromisoformat(violation["T1"])
    t2 = date.fromisoformat(violation["T2"])

    trade = Trade(
        episode_id=_episode_id_for(underlying, violation),
        underlying=underlying,
        denomination=denom,
        sizing=cfg.sizing,
        exit_mode=cfg.exit_mode,
        entry_ts=violation["ts"],
        long_symbol=violation[f"sym_{long_leg}"],
        short_symbol=violation[f"sym_{short_leg}"],
        T1=t1,
        T2=t2,
        violation_size=float(violation["violation_size"]),
    )

    # -- entry ------------------------------------------------------------
    entry_long = float(violation[f"{long_leg}_ask"])   # we buy
    entry_short = float(violation[f"{short_leg}_bid"])  # we sell
    if cfg.sizing is Sizing.RATIO:
        # The study's continuous weights. Reported for comparison with the
        # paper only - options trade in whole contracts, so these quantities
        # cannot be sent to a broker and the resulting P&L is not achievable.
        qty_long = float(violation[f"{weight_leg}_ask"])
        qty_short = float(violation["C_bid"])
        trade.note = (trade.note + " | " if trade.note else "") + (
            "RATIO sizing: fractional contracts, theoretical only, not executable"
        )
    else:
        qty_long = qty_short = 1.0

    trade.qty_long, trade.qty_short = qty_long, qty_short
    trade.executable = (
        float(qty_long).is_integer() and float(qty_short).is_integer()
    )
    trade.entry_long_price, trade.entry_short_price = entry_long, entry_short
    trade.entry_cash = qty_short * entry_short - qty_long * entry_long

    # -- which exit is available -----------------------------------------
    near_expiry = t1 if denom is Denomination.T1 else min(t1, t2)
    can_expire = near_expiry <= cfg.competition_end
    mode = cfg.exit_mode
    if cfg.prefer_expiry_when_possible and can_expire:
        mode = ExitMode.EXPIRATION
    elif mode is ExitMode.EXPIRATION and not can_expire:
        mode = ExitMode.REVERSION
        trade.note = (
            f"near leg expires {near_expiry.isoformat()}, after the competition "
            f"ends {cfg.competition_end.isoformat()}; cannot hold to expiry"
        )
    trade.exit_mode = mode

    # -- exit -------------------------------------------------------------
    if mode is ExitMode.EXPIRATION:
        if terminal_price is None:
            trade.note = (trade.note + " | " if trade.note else "") + (
                "expiry settlement requires a terminal underlying price; "
                "expiry has not occurred"
            )
            return trade
        k_long = float(violation["K1" if long_leg == "A" else "K2"])
        k_short = float(violation["K2_adj"])
        payoff_long = max(terminal_price - k_long, 0.0)
        payoff_short = max(terminal_price - k_short, 0.0)
        trade.exit_long_price = payoff_long
        trade.exit_short_price = payoff_short
        trade.exit_cash = qty_long * payoff_long - qty_short * payoff_short
        trade.exit_ts = near_expiry.isoformat()
        trade.settled_at_expiry = True
        trade.resolved = True
    else:
        exit_row = _find_exit(path, mode)
        if exit_row is None:
            trade.note = (trade.note + " | " if trade.note else "") + (
                "no exit observation available; episode still open"
            )
            return trade
        # Close: sell the long at bid, buy back the short at ask.
        trade.exit_long_price = float(exit_row[f"{long_leg}_bid"] or 0.0)
        trade.exit_short_price = float(exit_row[f"{short_leg}_ask"] or 0.0)
        trade.exit_cash = (
            qty_long * trade.exit_long_price - qty_short * trade.exit_short_price
        )
        trade.exit_ts = exit_row["ts"]
        trade.resolved = True

    n_contracts = 2
    trade.costs = (
        n_contracts * 2 * cfg.cost_per_contract_side * max(qty_long, qty_short)
    )
    return trade


def _find_exit(path: list[dict], mode: ExitMode) -> dict | None:
    """First non-violating observation with usable quotes, or the last one."""
    usable = [
        r
        for r in path
        if r.get("observable") == "1" and (r.get("A_bid") or "") != ""
    ]
    if not usable:
        return None
    if mode is ExitMode.REVERSION:
        for row in usable[1:]:
            if row.get("violating") == "0":
                return row
        return None
    return usable[-1]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def run(
    data_dir: Path,
    underlying: str,
    cfg: BacktestConfig,
    terminal_price: float | None = None,
) -> list[Trade]:
    violations = load_violations(data_dir / "violations.csv")
    paths = load_paths(data_dir / "episode_path.csv")

    # One trade per episode: the first time each rectangle was detected.
    seen: set[str] = set()
    trades: list[Trade] = []
    for row in violations:
        ep = _episode_id_for(underlying, row)
        if ep in seen:
            continue
        seen.add(ep)
        for denom in (Denomination.T1, Denomination.K2):
            trades.append(
                simulate(row, paths.get(ep, []), denom, cfg, underlying, terminal_price)
            )
    return trades


def summarise(trades: list[Trade]) -> dict:
    resolved = [t for t in trades if t.resolved]
    wins = [t for t in resolved if t.net_pnl > 0]
    total = sum(t.net_pnl for t in resolved)
    holds = sorted(t.holding_seconds for t in resolved if t.holding_seconds > 0)
    return {
        "trades": len(trades),
        "resolved": len(resolved),
        "unresolved": len(trades) - len(resolved),
        "wins": len(wins),
        "hit_rate": len(wins) / len(resolved) if resolved else 0.0,
        "total_pnl": total,
        "mean_pnl": total / len(resolved) if resolved else 0.0,
        "median_hold_s": holds[len(holds) // 2] if holds else 0.0,
        "settled_at_expiry": sum(1 for t in resolved if t.settled_at_expiry),
    }

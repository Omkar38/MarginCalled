"""Order construction and submission. Nothing else may send an order.

The design turns a data weakness into a safety property.

Detection runs on Alpaca's indicative feed, whose quotes are model-generated and
demonstrably not internally consistent - same-expiry pairs have been observed
breaching the vertical no-arbitrage bound, and legs moving against the
underlying. But Alpaca's paper engine fills against real NBBO:

    "all orders submitted in paper trading will be matched against the best
     available current market price (NBBO)"

So the signal is synthetic while the fill is real. Sending a market order on
that basis would convert a phantom signal directly into a real position.

Instead every order is a LIMIT order priced *conservatively against the
indicative quotes*: we demand terms strictly better than the phantom quote
implies. If the real market is genuinely at least that good, the order fills at
a price we modelled. If the indicative quote was an artefact, the order is
simply not marketable and never fills.

Bad data therefore produces non-fills, not bad fills. That is a deliberate risk
control, not a workaround, and it is why market orders are refused outright.

SAFETY
  - Refuses any host but paper-api, and refuses live (AK...) keys.
  - Refuses to submit without an approved RiskDecision.
  - Limit orders only; `type: market` is never constructed.
  - Dry-run by default: build_order returns a payload, submit() must be called
    explicitly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from .alpaca import LIVE_HOST, TRADING_HOST, AlpacaDataClient, AlpacaError
from .position import Side, PositionSpec
from .risk import RiskDecision

__all__ = [
    "ExecutionError",
    "Transport",
    "LimitPolicy",
    "OrderPlan",
    "Executor",
]


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LimitPolicy:
    """How far better than the indicative quote we insist on being filled.

    The shade is expressed in units of the position's own quoted spread rather
    than as a flat percentage: a wide, uncertain market demands a bigger
    concession than a tight one, which is the correct scaling. `shade_spreads =
    1.0` requires the real market to be one full package-spread better than the
    indicative quote suggested before the order can fill.
    """

    shade_spreads: float = 1.0
    min_shade_abs: float = 0.01  # never demand less than one tick
    round_to: float = 0.01


@dataclass
class OrderPlan:
    """A ready-to-send MLeg order, with the reasoning that produced its price."""

    legs: list[dict] = field(default_factory=list)
    indicative_net: float = 0.0   # >0 = we pay a debit, <0 = we receive a credit
    package_spread: float = 0.0
    shade: float = 0.0
    limit_price: float = 0.0
    is_debit: bool = True
    qty: int = 1
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "order_class": "mleg",
            "qty": str(self.qty),
            "type": "limit",
            "time_in_force": "day",
            # Alpaca's MLeg convention: positive = debit (we pay), negative =
            # credit (we receive). The sign must be preserved - taking the
            # absolute value would submit a credit spread as a debit.
            "limit_price": f"{self.limit_price:.2f}",
            "legs": self.legs,
        }

    def to_record(self) -> dict:
        return {
            "indicative_net": self.indicative_net,
            "package_spread": self.package_spread,
            "shade": self.shade,
            "limit_price": self.limit_price,
            "is_debit": self.is_debit,
            "qty": self.qty,
            "legs": list(self.legs),
            "notes": list(self.notes),
        }


def build_order(
    spec: PositionSpec, policy: LimitPolicy | None = None, qty: int = 1
) -> OrderPlan:
    """Price the package conservatively against the indicative quotes.

    The indicative net is what the phantom quotes say the package costs, taken
    on the sides a trade must cross - buys at ask, sells at bid. The shade moves
    the limit against us relative to that number, so a fill requires the real
    NBBO to be better than the indicative feed claimed.
    """
    policy = policy or LimitPolicy()
    if not spec.is_executable:
        raise ExecutionError(spec.rejected_reason or "position is not executable")

    net = 0.0
    spread = 0.0
    legs: list[dict] = []
    for leg in spec.legs:
        quote = _leg_quote(spec, leg.symbol)
        if leg.side is Side.BUY:
            net += leg.entry_price * leg.ratio_qty      # we pay the ask
        else:
            net -= leg.entry_price * leg.ratio_qty      # we receive the bid
        spread += abs(quote[1] - quote[0]) * leg.ratio_qty
        legs.append(leg.to_alpaca_leg())

    shade = max(policy.shade_spreads * spread, policy.min_shade_abs)
    is_debit = net > 0

    if is_debit:
        # We pay. Insist on paying less than the indicative quote implied.
        limit = net - shade
    else:
        # We receive. Insist on receiving more than it implied.
        limit = net - shade  # net is negative; subtracting makes it more negative

    step = policy.round_to
    limit = round(limit / step) * step

    plan = OrderPlan(
        legs=legs,
        indicative_net=net,
        package_spread=spread,
        shade=shade,
        limit_price=limit,
        is_debit=is_debit,
        qty=qty,
    )
    plan.notes.append(
        f"indicative net {net:+.4f} on a {spread:.4f} package spread; shaded by "
        f"{shade:.4f} to {limit:+.4f}. A fill requires the real NBBO to be at "
        f"least this much better than the indicative quote implied."
    )
    if is_debit and limit <= 0:
        plan.notes.append(
            "shade exceeds the debit; the limit is now a credit and will almost "
            "certainly not fill. That is the intended failure mode."
        )
    return plan


def _leg_quote(spec: PositionSpec, symbol: str) -> tuple[float, float]:
    for leg in (spec.candidate.A, spec.candidate.B, spec.candidate.C, spec.candidate.D):
        if leg.symbol == symbol:
            return leg.quote.bid, leg.quote.ask
    raise ExecutionError(f"no quote for leg {symbol}")


class Transport(str, Enum):
    """How the order reaches Alpaca.

    MCP is the default. The competition requires the Trading API plus either the
    MCP server or the CLI, and routing orders through MCP removes any argument
    about whether the requirement is met. It costs nothing in safety: this is a
    direct tool call from our own code, not a language model deciding to trade.
    REST is kept as a fallback for when the MCP server is unavailable.
    """

    MCP = "mcp"
    REST = "rest"


class Executor:
    """Submits MLeg orders. Requires an approved RiskDecision for every send."""

    def __init__(
        self,
        client: AlpacaDataClient,
        live_ok: bool = False,
        transport: "Transport" = None,
        mcp: object = None,
    ) -> None:
        self.c = client
        self.transport = transport or Transport.MCP
        self.mcp = mcp
        if live_ok:
            raise ExecutionError("live trading is not supported by this project")

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{TRADING_HOST}{path}"
        if url.startswith(LIVE_HOST):
            raise ExecutionError("REFUSED: live trading host")
        if not url.startswith(TRADING_HOST):
            raise ExecutionError(f"REFUSED: unknown host {url}")
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

    def submit(
        self, plan: OrderPlan, decision: RiskDecision, dry_run: bool = True
    ) -> dict:
        """Send the order. Refuses unless the risk gates approved it."""
        if not decision.approved:
            codes = ", ".join(code.value for code, _ in decision.rejections)
            raise ExecutionError(f"risk gates did not approve: {codes}")
        payload = plan.to_payload()
        if payload.get("type") != "limit":
            raise ExecutionError("only limit orders may be sent")
        if dry_run:
            return {"dry_run": True, "transport": self.transport.value,
                    "payload": payload,
                    "submitted_at": datetime.now().isoformat(timespec="seconds")}

        stamp = datetime.now().isoformat(timespec="seconds")
        if self.transport is Transport.MCP:
            if self.mcp is None:
                raise ExecutionError(
                    "transport is MCP but no MCP client was supplied; construct "
                    "Executor(..., mcp=AlpacaMCPClient(toolsets=TRADING_TOOLSETS))"
                )
            text = self.mcp.place_option_order(payload)
            return {
                "dry_run": False,
                "transport": "mcp",
                "response": text,
                "payload": payload,
                "submitted_at": stamp,
            }

        status, data = self._request("POST", "/v2/orders", payload)
        return {
            "dry_run": False,
            "transport": "rest",
            "status": status,
            "accepted": status in (200, 201),
            "order_id": data.get("id"),
            "response": data,
            "payload": payload,
            "submitted_at": stamp,
        }

    def cancel(self, order_id: str) -> tuple[int, dict]:
        return self._request("DELETE", f"/v2/orders/{order_id}")

    def order(self, order_id: str) -> tuple[int, dict]:
        return self._request("GET", f"/v2/orders/{order_id}")

    def open_orders(self) -> list:
        status, data = self._request("GET", "/v2/orders?status=open&limit=100")
        return data if isinstance(data, list) else []

    def positions(self) -> list:
        status, data = self._request("GET", "/v2/positions")
        return data if isinstance(data, list) else []

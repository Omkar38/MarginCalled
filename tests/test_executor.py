"""Tests for order construction and the conservative limit policy."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tp2agent.executor import (  # noqa: E402
    ExecutionError,
    Executor,
    LimitPolicy,
    Transport,
    build_order,
)
from tp2agent.position import PositionConfig, Structure, build_position  # noqa: E402
from tp2agent.risk import RejectCode, RiskDecision  # noqa: E402

from test_position import _realistic_candidate  # noqa: E402


def _spec():
    return build_position(
        _realistic_candidate(), PositionConfig(structure=Structure.FOUR_LEG)
    )


def _approved() -> RiskDecision:
    d = RiskDecision()
    d.approved = True
    return d


def _rejected() -> RiskDecision:
    d = RiskDecision()
    d.reject(RejectCode.DAILY_STOP, "stopped")
    d.approved = False
    return d


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_indicative_net_uses_crossable_sides():
    """Buys priced at ask, sells at bid - the sides a trade must cross."""
    spec = _spec()
    plan = build_order(spec)
    expected = 0.0
    for leg in spec.legs:
        expected += leg.entry_price * leg.ratio_qty * (1 if leg.side.value == "buy" else -1)
    assert abs(plan.indicative_net - expected) < 1e-9


def test_limit_is_shaded_against_us():
    """The limit must demand better terms than the indicative quote implied."""
    plan = build_order(_spec(), LimitPolicy(shade_spreads=1.0))
    assert plan.shade > 0
    # Whether debit or credit, the limit moves in the direction that makes a
    # fill harder, never easier.
    assert plan.limit_price < plan.indicative_net


def test_bigger_spread_demands_bigger_concession():
    """Shade scales with the package spread, not a flat percentage."""
    tight = build_order(_spec(), LimitPolicy(shade_spreads=0.5))
    wide = build_order(_spec(), LimitPolicy(shade_spreads=2.0))
    assert wide.shade > tight.shade
    assert wide.limit_price < tight.limit_price


def test_shade_never_below_one_tick():
    plan = build_order(_spec(), LimitPolicy(shade_spreads=0.0, min_shade_abs=0.01))
    assert plan.shade >= 0.01


def test_limit_is_rounded_to_a_tick():
    plan = build_order(_spec(), LimitPolicy(round_to=0.01))
    assert abs(plan.limit_price * 100 - round(plan.limit_price * 100)) < 1e-6


def test_payload_is_always_a_limit_order():
    payload = build_order(_spec()).to_payload()
    assert payload["type"] == "limit"
    assert payload["order_class"] == "mleg"
    assert "limit_price" in payload
    assert len(payload["legs"]) <= 4


def test_unexecutable_position_is_refused():
    spec = _spec()
    spec.rejected_reason = "uncovered"
    try:
        build_order(spec)
    except ExecutionError as exc:
        assert "uncovered" in str(exc)
        return
    raise AssertionError("an unexecutable position must be refused")


def test_plan_records_its_reasoning():
    plan = build_order(_spec())
    assert plan.notes
    assert any("real NBBO" in n for n in plan.notes)


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------


class _StubClient:
    key = "PKTEST0000000000"
    secret = "s"


def test_submit_refuses_without_risk_approval():
    ex = Executor(_StubClient())
    plan = build_order(_spec())
    try:
        ex.submit(plan, _rejected(), dry_run=True)
    except ExecutionError as exc:
        assert "risk gates did not approve" in str(exc)
        assert "daily_stop" in str(exc)
        return
    raise AssertionError("submission without approval must be refused")


def test_dry_run_does_not_send():
    ex = Executor(_StubClient())
    out = ex.submit(build_order(_spec()), _approved(), dry_run=True)
    assert out["dry_run"] is True
    assert out["payload"]["type"] == "limit"
    assert "order_id" not in out


def test_live_mode_is_refused_at_construction():
    try:
        Executor(_StubClient(), live_ok=True)
    except ExecutionError as exc:
        assert "live trading is not supported" in str(exc)
        return
    raise AssertionError("live mode must be refused")


def test_live_host_is_refused():
    ex = Executor(_StubClient())
    try:
        ex._request("GET", "/v2/account")  # fine
    except ExecutionError:
        pass
    # Directly attempt a live URL through the guard.
    import tp2agent.executor as mod

    saved = mod.TRADING_HOST
    try:
        mod.TRADING_HOST = mod.LIVE_HOST
        try:
            ex._request("POST", "/v2/orders", {})
        except ExecutionError as exc:
            assert "live trading host" in str(exc).lower()
            return
        raise AssertionError("the live host must be refused")
    finally:
        mod.TRADING_HOST = saved


def test_limit_price_sign_is_preserved():
    """Alpaca MLeg: positive = debit, negative = credit.

    Taking the absolute value would submit a credit spread as a debit - an order
    to PAY what we intended to RECEIVE.
    """
    plan = build_order(_spec())
    plan.limit_price = -1.25
    assert plan.to_payload()["limit_price"] == "-1.25"
    plan.limit_price = 2.50
    assert plan.to_payload()["limit_price"] == "2.50"


def test_default_transport_is_mcp():
    ex = Executor(_StubClient())
    assert ex.transport is Transport.MCP


def test_mcp_transport_without_client_is_refused():
    ex = Executor(_StubClient(), transport=Transport.MCP)
    try:
        ex.submit(build_order(_spec()), _approved(), dry_run=False)
    except ExecutionError as exc:
        assert "no MCP client was supplied" in str(exc)
        return
    raise AssertionError("MCP transport without a client must be refused")


def test_mcp_submission_goes_through_the_tool():
    class _StubMCP:
        def __init__(self):
            self.sent = None

        def place_option_order(self, payload):
            self.sent = payload
            return '{"id": "stub-order-1"}'

    mcp = _StubMCP()
    ex = Executor(_StubClient(), transport=Transport.MCP, mcp=mcp)
    out = ex.submit(build_order(_spec()), _approved(), dry_run=False)
    assert out["transport"] == "mcp"
    assert mcp.sent is not None, "the MCP tool was not called"
    assert mcp.sent["order_class"] == "mleg"
    assert mcp.sent["type"] == "limit"


def test_mcp_transport_still_requires_risk_approval():
    class _StubMCP:
        def place_option_order(self, payload):
            raise AssertionError("must not be reached without approval")

    ex = Executor(_StubClient(), transport=Transport.MCP, mcp=_StubMCP())
    try:
        ex.submit(build_order(_spec()), _rejected(), dry_run=False)
    except ExecutionError as exc:
        assert "risk gates did not approve" in str(exc)
        return
    raise AssertionError("approval must gate MCP submission too")


def test_module_never_constructs_a_market_order():
    src = (
        Path(__file__).resolve().parents[1] / "src" / "tp2agent" / "executor.py"
    ).read_text()
    assert '"market"' not in src, "a market order type must never appear"
    assert '"limit"' in src


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

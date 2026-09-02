"""Tests for exit management."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.exits import (  # noqa: E402
    ExitPolicy,
    ExitReason,
    OpenPosition,
    build_close_legs,
    should_exit,
)

NOW = datetime(2026, 9, 2, 11, 0)


def _pos(opened_minutes_ago: int = 10, near_expiry: date = date(2026, 10, 16)):
    return OpenPosition(
        episode_id="ep1",
        underlying="SPX",
        denomination="T1",
        order_id="ord-1",
        opened_at=NOW - timedelta(minutes=opened_minutes_ago),
        long_symbol="SPX261016C07000000",
        short_symbol="SPX261016C07050000",
        long_expiry=near_expiry,
        short_expiry=near_expiry,
        entry_long_price=10.0,
        entry_short_price=8.0,
    )


# --------------------------------------------------------------------------
# Trigger precedence
# --------------------------------------------------------------------------


def test_reverted_closes():
    d = should_exit(_pos(), "reverted", NOW)
    assert d.should_close
    assert d.reason is ExitReason.REVERTED
    assert not d.urgent


def test_active_episode_holds():
    d = should_exit(_pos(), "active", NOW)
    assert not d.should_close
    assert d.reason is ExitReason.HOLD


def test_unknown_status_holds_rather_than_closing():
    """An untracked episode is not evidence the thesis resolved."""
    d = should_exit(_pos(), None, NOW)
    assert not d.should_close


def test_time_stop_closes_an_unreverted_position():
    d = should_exit(_pos(opened_minutes_ago=200), "active",
                    NOW, ExitPolicy(max_hold_minutes=120))
    assert d.should_close
    assert d.reason is ExitReason.TIME_STOP


def test_deadline_outranks_everything():
    """Even an active episode well inside the time stop must close."""
    policy = ExitPolicy(flatten_after=NOW - timedelta(minutes=1))
    d = should_exit(_pos(opened_minutes_ago=1), "active", NOW, policy)
    assert d.should_close
    assert d.reason is ExitReason.DEADLINE
    assert d.urgent, "deadline exits must relax the price shading"


def test_near_expiry_closes_and_is_urgent():
    d = should_exit(_pos(near_expiry=date(2026, 9, 3)), "active", NOW)
    assert d.should_close
    assert d.reason is ExitReason.EXPIRY_NEAR
    assert d.urgent


def test_expiry_check_uses_the_nearer_leg():
    pos = _pos()
    pos.long_expiry = date(2026, 12, 18)
    pos.short_expiry = date(2026, 9, 3)
    assert pos.near_expiry == date(2026, 9, 3)
    assert should_exit(pos, "active", NOW).reason is ExitReason.EXPIRY_NEAR


def test_deadline_beats_expiry():
    policy = ExitPolicy(flatten_after=NOW - timedelta(minutes=1))
    d = should_exit(_pos(near_expiry=date(2026, 9, 3)), "active", NOW, policy)
    assert d.reason is ExitReason.DEADLINE


# --------------------------------------------------------------------------
# Closing structure
# --------------------------------------------------------------------------


def test_close_inverts_the_entry():
    pos = _pos()
    legs = build_close_legs(pos)
    assert len(legs) == 2
    by_symbol = {leg["symbol"]: leg for leg in legs}
    assert by_symbol[pos.long_symbol]["side"] == "sell"
    assert by_symbol[pos.long_symbol]["position_intent"] == "sell_to_close"
    assert by_symbol[pos.short_symbol]["side"] == "buy"
    assert by_symbol[pos.short_symbol]["position_intent"] == "buy_to_close"


def test_close_is_one_order_not_two():
    """Legging out would leave the short uncovered between fills."""
    legs = build_close_legs(_pos())
    assert len(legs) == 2, "both legs must travel in a single order"
    assert {leg["ratio_qty"] for leg in legs} == {"1"}


def test_close_preserves_quantity():
    pos = _pos()
    pos.qty = 3
    legs = build_close_legs(pos)
    assert all(leg["ratio_qty"] == "3" for leg in legs)


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------


def test_open_position_lifecycle_flags():
    pos = _pos()
    assert pos.is_open
    pos.closed_at = NOW
    assert not pos.is_open


def test_held_minutes():
    assert abs(_pos(opened_minutes_ago=45).held_minutes(NOW) - 45) < 1e-6


def test_record_is_json_serialisable():
    import json

    text = json.dumps(_pos().to_record())
    assert "SPX261016C07000000" in text


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

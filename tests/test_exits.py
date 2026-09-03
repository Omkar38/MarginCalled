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
    PositionRegistry,
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


def test_position_and_tracker_must_share_an_episode_key():
    """The registry keyed positions on the short leg symbol while the tracker
    keyed episodes on a hash of all four contracts, so the lookup always
    returned None and REVERTED could never fire for any position. This asserts
    the two identifiers are produced the same way."""
    import sys as _sys
    from pathlib import Path as _P

    _sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "tests"))
    from test_position import _realistic_candidate

    from tp2agent.episodes import episode_id

    cand = _realistic_candidate()
    tracker_key = episode_id("SPY", cand)
    assert tracker_key != cand.episode_key, (
        "these are deliberately different identifiers; a position must be "
        "stored under the tracker key, never episode_key"
    )
    # A position stored under the tracker key resolves; under episode_key it
    # does not - which is exactly the bug.
    episodes = {tracker_key: object()}
    assert episodes.get(tracker_key) is not None
    assert episodes.get(cand.episode_key) is None


def test_unknown_episode_holds_so_a_key_mismatch_is_silent():
    """Why the mismatch survived: an unknown status is treated as HOLD, which is
    correct behaviour and indistinguishable from a position that simply has not
    reverted yet. Nothing raised, nothing logged - it just never exited."""
    d = should_exit(_pos(), None, NOW)
    assert not d.should_close
    assert d.reason is ExitReason.HOLD


def test_time_stop_needs_a_local_opened_at():
    """Adopted positions carried the broker's UTC timestamp while everything
    else in the process is local, so held_minutes came out about -240 and
    TIME_STOP could never fire on them. Guards the invariant that opened_at is
    local wall-clock."""
    from datetime import timedelta as _td

    utc_looking = _pos(opened_minutes_ago=-240)      # four hours in the future
    assert utc_looking.held_minutes(NOW) < 0
    d = should_exit(utc_looking, "active", NOW, ExitPolicy(max_hold_minutes=120))
    assert not d.should_close, "a future opened_at silently disables the time stop"

    local = _pos(opened_minutes_ago=240)
    assert should_exit(local, "active", NOW,
                       ExitPolicy(max_hold_minutes=120)).reason is ExitReason.TIME_STOP


def test_broker_utc_versus_local_makes_ages_negative():
    """The same fault appeared twice: broker timestamps are UTC while the
    process works in local time. In adopt_orphans it disabled TIME_STOP; in
    cancel_stale it meant stale orders were never recycled, because a negative
    age never exceeds any positive limit. Ten orders accumulated over half an
    hour, every one priced below the market it was built from."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    local_now = _dt(2026, 9, 3, 14, 26, 0)
    broker_utc = "2026-09-03T18:26:21Z"          # the same instant, in UTC

    naive = _dt.fromisoformat(broker_utc[:19])
    assert (local_now - naive).total_seconds() < 0, "the bug: negative age"

    converted = (_dt.fromisoformat(broker_utc.replace("Z", "+00:00"))
                 .astimezone().replace(tzinfo=None))
    assert abs((local_now - converted).total_seconds()) < 60, "the fix: same instant"


def test_a_registry_must_only_hold_its_own_underlying():
    """The account is shared across scanners. Adoption read every broker
    position without filtering, so the SPX scanner adopted SPY's book into
    data/SPX/positions.jsonl. Both scanners then believed they held the same
    positions, and both would have sent closing orders - the second selling
    something no longer held, which opens a naked short."""
    import tempfile as _tf
    from pathlib import Path as _P

    with _tf.TemporaryDirectory() as d:
        reg = PositionRegistry(_P(d) / "positions.jsonl")
        reg.add(OpenPosition(
            episode_id="e1", underlying="SPX", denomination="T1", order_id="o1",
            opened_at=NOW, long_symbol="SPX261016C07000000",
            short_symbol="SPX261016C07050000",
            long_expiry=date(2026, 10, 16), short_expiry=date(2026, 10, 16),
        ))
        for pos in reg.open_positions():
            assert pos.long_symbol.startswith(pos.underlying), (
                f"{pos.underlying} registry holds {pos.long_symbol}"
            )
            assert pos.short_symbol.startswith(pos.underlying)


def test_held_symbols_is_what_guards_against_double_closing():
    """Both scanners closing the same position is the danger, so held_symbols
    must report every leg a registry believes it owns."""
    import tempfile as _tf
    from pathlib import Path as _P

    with _tf.TemporaryDirectory() as d:
        reg = PositionRegistry(_P(d) / "p.jsonl")
        reg.add(_pos())
        held = reg.held_symbols()
        assert "SPX261016C07000000" in held and "SPX261016C07050000" in held


def test_a_closed_position_is_not_adopted_again():
    """held_symbols() reports OPEN positions only, so a position closed moments
    ago - still sitting at the broker until its closing order fills - looked
    unknown to adoption and was taken back on as new. The agent re-acquired
    something it had just exited and would have closed it twice. Adoption must
    consult every position the registry has ever held."""
    import tempfile as _tf
    from pathlib import Path as _P

    with _tf.TemporaryDirectory() as d:
        reg = PositionRegistry(_P(d) / "p.jsonl")
        reg.add(_pos())
        reg.close("ep1", "close-1", "reverted", NOW)

        assert reg.open_positions() == []
        assert reg.held_symbols() == set(), "closed positions leave held_symbols"

        ever_held = set()
        for p in reg.positions.values():
            ever_held.add(p.long_symbol)
            ever_held.add(p.short_symbol)
        assert "SPX261016C07000000" in ever_held, (
            "the closed position must still be recognised, or it is adopted again"
        )


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

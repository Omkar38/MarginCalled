"""Tests for episode lifecycle tracking."""

from __future__ import annotations

import csv
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tp2agent.episodes import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_REVERTED,
    EpisodeTracker,
    episode_id,
    remeasure,
)
from tp2agent.rectangles import OptionQuote  # noqa: E402

from test_rectangles import R, T1, T2, _clean_chain, _q  # noqa: E402
from test_position import _realistic_candidate  # noqa: E402

T0 = datetime(2026, 8, 31, 10, 0, 0)


def _cand(severity: float = 0.05):
    """A candidate whose normalized severity is controllable."""
    c = _realistic_candidate()
    rhs = c.rhs
    return replace(c, violation_size=severity * rhs)


def _tracker(tmp: Path, revert_after: int = 2) -> EpisodeTracker:
    return EpisodeTracker(tmp, "SPY", revert_after=revert_after)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_episode_id_uses_all_four_legs():
    """Two rectangles sharing a short leg must not collapse into one episode."""
    c1 = _realistic_candidate()
    c2 = replace(c1, B=OptionQuote("OTHER_B", c1.B.strike, c1.B.expiry, "C", c1.B.quote))
    assert episode_id("SPY", c1) != episode_id("SPY", c2)


def test_episode_id_is_stable_and_underlying_scoped():
    c = _realistic_candidate()
    assert episode_id("SPY", c) == episode_id("SPY", c)
    assert episode_id("SPY", c) != episode_id("SPX", c)


# --------------------------------------------------------------------------
# Re-pricing
# --------------------------------------------------------------------------


def test_remeasure_recovers_the_determinant():
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d))
        chain = _clean_chain()
        cand = _cand(0.05)
        tr.observe(T0, chain, [cand], {})
        ep = next(iter(tr.episodes.values()))
        m = remeasure(chain, ep)
        assert m.observable
        expected = cand.C.quote.bid * cand.D.quote.bid
        assert abs(m.rhs - expected) < 1e-9


def test_remeasure_reports_missing_legs_rather_than_reverting():
    """A leg dropping out of the chain must not look like a reversion."""
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d))
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        ep = next(iter(tr.episodes.values()))
        del chain.calls[ep.T1][ep.K1]
        m = remeasure(chain, ep)
        assert not m.observable
        assert "A" in m.missing


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_new_episode_is_recorded():
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d))
        stats = tr.observe(T0, _clean_chain(), [_cand(0.05)], {})
        assert stats["new"] == 1
        ep = next(iter(tr.episodes.values()))
        assert ep.status == STATUS_ACTIVE
        assert ep.observations == 1
        assert abs(ep.first_severity - 0.05) < 1e-9
        assert abs(ep.peak_severity - 0.05) < 1e-9


def test_continuing_episode_updates_peak():
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d))
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.03)], {})
        tr.observe(T0 + timedelta(minutes=5), chain, [_cand(0.09)], {})
        tr.observe(T0 + timedelta(minutes=10), chain, [_cand(0.04)], {})
        ep = next(iter(tr.episodes.values()))
        assert ep.violating_observations == 3
        assert abs(ep.peak_severity - 0.09) < 1e-9
        assert ep.peak_at == T0 + timedelta(minutes=5)
        assert abs(ep.last_severity - 0.04) < 1e-9
        assert ep.status == STATUS_ACTIVE


def test_episode_reverts_after_consecutive_clean_scans():
    """The clean chain does not violate, so absence + re-pricing => reversion."""
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d), revert_after=2)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        ep = next(iter(tr.episodes.values()))

        s1 = tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        assert s1["reverted"] == 0, "one clean scan must not close an episode"
        assert ep.status == STATUS_ACTIVE

        s2 = tr.observe(T0 + timedelta(minutes=10), chain, [], {})
        assert s2["reverted"] == 1
        assert ep.status == STATUS_REVERTED
        assert ep.reverted_at == T0 + timedelta(minutes=10)
        assert ep.duration_seconds == 600


def test_single_clean_scan_does_not_revert():
    """A transient stale quote must not close a live episode."""
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d), revert_after=3)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        tr.observe(T0 + timedelta(minutes=10), chain, [_cand(0.05)], {})
        ep = next(iter(tr.episodes.values()))
        assert ep.status == STATUS_ACTIVE
        assert ep.violating_observations == 2


def test_reverted_episode_reopens_as_new():
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d), revert_after=1)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        assert next(iter(tr.episodes.values())).status == STATUS_REVERTED
        stats = tr.observe(T0 + timedelta(minutes=10), chain, [_cand(0.07)], {})
        assert stats["new"] == 1
        assert next(iter(tr.episodes.values())).status == STATUS_ACTIVE


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_path_file_records_every_observation():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tr = _tracker(tmp)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        tr.observe(T0 + timedelta(minutes=10), chain, [], {})
        rows = list(csv.DictReader((tmp / "episode_path.csv").open()))
        assert len(rows) == 3
        assert [int(r["event_index"]) for r in rows] == [0, 1, 2]
        assert int(rows[0]["violating"]) == 1
        assert int(rows[2]["violating"]) == 0


def test_episode_file_is_rewritten_with_state():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tr = _tracker(tmp, revert_after=1)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        rows = list(csv.DictReader((tmp / "episodes.csv").open()))
        assert len(rows) == 1
        assert rows[0]["status"] == STATUS_REVERTED
        assert rows[0]["reverted_at"]
        assert float(rows[0]["duration_seconds"]) == 300


def test_summary_reports_durations():
    with tempfile.TemporaryDirectory() as d:
        tr = _tracker(Path(d), revert_after=1)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        s = tr.summary()
        assert s["total"] == 1 and s["reverted"] == 1 and s["active"] == 0
        assert s["median_duration_s"] == 300
        assert abs(s["peak_severity"] - 0.05) < 1e-9


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

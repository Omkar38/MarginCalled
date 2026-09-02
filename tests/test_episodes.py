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
    PATH_FIELDS,
    _migrate_csv_header,
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
    """A candidate whose normalized severity is controllable.

    Solved for the study's convention, severity = (rhs - lhs) / (lhs + rhs):

        (rhs - lhs) / (lhs + rhs) = s   =>   lhs = rhs * (1 - s) / (1 + s)

    lhs and violation_size are both set so the three fields stay mutually
    consistent (violation_size == rhs - lhs). The previous fixture set only
    violation_size, as `severity * rhs`, which both hard-coded the old /rhs
    convention and left lhs contradicting violation_size.
    """
    c = _realistic_candidate()
    rhs = c.rhs
    lhs = rhs * (1.0 - severity) / (1.0 + severity)
    return replace(c, lhs=lhs, violation_size=rhs - lhs)


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


def test_path_records_per_leg_quotes_at_every_observation():
    """Exit prices must be recoverable, not just the products.

    lhs and rhs are A_ask*B_ask and C_bid*D_bid; the individual legs cannot be
    recovered from them. Without per-leg quotes at reversion a backtest has
    entry prices and no exit, so no P&L can be computed.
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tr = _tracker(tmp)
        chain = _clean_chain()
        cand = _cand(0.05)
        tr.observe(T0, chain, [cand], {})                      # entry
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})   # re-priced, no longer violating

        rows = list(csv.DictReader((tmp / "episode_path.csv").open()))
        assert len(rows) == 2
        for row in rows:
            for lbl in "ABCD":
                for side in ("bid", "ask"):
                    val = float(row[f"{lbl}_{side}"])
                    assert val == val, f"{lbl}_{side} is nan"
                    assert val > 0, f"{lbl}_{side} not positive"

        # Entry row must match the candidate's own quotes.
        assert abs(float(rows[0]["A_ask"]) - cand.A.quote.ask) < 1e-5
        assert abs(float(rows[0]["D_bid"]) - cand.D.quote.bid) < 1e-5
        # Exit row is a fresh re-pricing of the same four contracts.
        exit_c = chain.calls[cand.T2][cand.K1_adj].quote.bid
        assert abs(float(rows[1]["C_bid"]) - exit_c) < 1e-5


def test_missing_legs_have_no_quotes_but_still_log():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tr = _tracker(tmp)
        chain = _clean_chain()
        tr.observe(T0, chain, [_cand(0.05)], {})
        ep = next(iter(tr.episodes.values()))
        del chain.calls[ep.T1][ep.K1]
        tr.observe(T0 + timedelta(minutes=5), chain, [], {})
        rows = list(csv.DictReader((tmp / "episode_path.csv").open()))
        assert int(rows[1]["observable"]) == 0
        assert "A" in rows[1]["missing_legs"]


def test_header_migration_widens_without_losing_rows():
    """An older narrow header must be widened, not left to mislabel new rows.

    Found live: episode_path.csv carried a 10-column header while the scanner
    appended 18-field rows. csv.DictReader dropped the surplus into a None key,
    so the leg quotes were in the file but invisible to any reader.
    """
    import csv as _csv

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "episode_path.csv"
        old_fields = PATH_FIELDS[:10]
        with path.open("w", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(old_fields)
            w.writerow(["ep1", "2026-09-01T10:00:00", "0", "1", "1",
                        "0.05", "1.0", "10.0", "11.0", ""])
        _migrate_csv_header(path, PATH_FIELDS)

        rows = list(_csv.DictReader(path.open()))
        assert len(rows) == 1
        assert rows[0]["episode_id"] == "ep1"
        assert rows[0]["severity"] == "0.05"
        assert rows[0]["A_bid"] == "", "old rows pad, not shift"
        assert rows[0].get(None) is None, "no unlabelled surplus"
        assert path.with_suffix(".csv.bak").exists(), "original must be kept"


def test_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.csv"
        _migrate_csv_header(path, PATH_FIELDS)
        _migrate_csv_header(path, PATH_FIELDS)
        import csv as _csv
        assert list(_csv.reader(path.open()))[0] == PATH_FIELDS


def test_migration_refuses_to_narrow():
    """Never silently drop columns."""
    import csv as _csv

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.csv"
        with path.open("w", newline="") as fh:
            _csv.writer(fh).writerow(PATH_FIELDS + ["extra_col"])
        try:
            _migrate_csv_header(path, PATH_FIELDS)
        except ValueError as exc:
            assert "refusing to drop" in str(exc)
            return
        raise AssertionError("narrowing must raise")


def test_state_survives_restart():
    """A restarted tracker must reload episodes, not erase them.

    flush() rewrites episodes.csv wholesale from the in-memory registry, so a
    tracker that started empty would wipe every episode recorded before the
    restart. Observed live: 27 SPX episodes lost on a scanner restart.
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tr1 = _tracker(tmp)
        tr1.observe(T0, _clean_chain(), [_cand(0.05)], {})
        ep_id = next(iter(tr1.episodes))
        assert len(tr1.episodes) == 1

        tr2 = _tracker(tmp)  # simulate a restart
        assert len(tr2.episodes) == 1, "episodes must reload from disk"
        assert ep_id in tr2.episodes
        restored = tr2.episodes[ep_id]
        assert restored.status == STATUS_ACTIVE
        assert abs(restored.peak_severity - 0.05) < 1e-9
        assert restored.first_seen == T0

        # And flushing after restart must not blank the file.
        tr2.flush()
        rows = list(csv.DictReader((tmp / "episodes.csv").open()))
        assert len(rows) == 1


def test_event_index_continues_after_restart():
    """Path indices must continue, not restart at zero."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        chain = _clean_chain()
        tr1 = _tracker(tmp)
        tr1.observe(T0, chain, [_cand(0.05)], {})
        tr1.observe(T0 + timedelta(minutes=5), chain, [_cand(0.06)], {})

        tr2 = _tracker(tmp)
        tr2.observe(T0 + timedelta(minutes=10), chain, [_cand(0.07)], {})
        rows = list(csv.DictReader((tmp / "episode_path.csv").open()))
        assert [int(r["event_index"]) for r in rows] == [0, 1, 2]


def test_reverted_episodes_are_preserved_across_restart():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        chain = _clean_chain()
        tr1 = _tracker(tmp, revert_after=1)
        tr1.observe(T0, chain, [_cand(0.05)], {})
        tr1.observe(T0 + timedelta(minutes=5), chain, [], {})
        assert next(iter(tr1.episodes.values())).status == STATUS_REVERTED

        tr2 = _tracker(tmp)
        assert len(tr2.episodes) == 1
        ep = next(iter(tr2.episodes.values()))
        assert ep.status == STATUS_REVERTED
        assert ep.reverted_at == T0 + timedelta(minutes=5)


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

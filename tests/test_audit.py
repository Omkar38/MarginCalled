"""Tests for the append-only decision log."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.audit import AuditLog, DecisionRecord, Outcome  # noqa: E402


def _log(tmp: Path) -> AuditLog:
    return AuditLog(tmp / "decisions.jsonl")


def _rec(**kw) -> DecisionRecord:
    base = dict(ts="2026-09-02T10:00:00", underlying="SPY", outcome=Outcome.ABSTAINED)
    base.update(kw)
    return DecisionRecord(**base)


# --------------------------------------------------------------------------
# Append-only
# --------------------------------------------------------------------------


def test_appending_never_replaces_earlier_records():
    with tempfile.TemporaryDirectory() as d:
        lg = _log(Path(d))
        lg.append(_rec(episode_key="first"))
        lg.append(_rec(episode_key="second"))
        keys = [r.episode_key for r in lg.read()]
        assert keys == ["first", "second"], "an append must not overwrite history"


def _code_only(path: Path) -> str:
    """Source with comments and docstrings stripped.

    The check below scans for truncating writes, so it must see code only. The
    prose in this module legitimately contains the words "truncate" and "w" -
    the first version of this test failed on its own explanatory comment.
    """
    import tokenize

    pieces = []
    prev = tokenize.INDENT
    with path.open("rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type == tokenize.COMMENT:
                continue
            is_docstring = tok.type == tokenize.STRING and prev in (
                tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.ENCODING,
            )
            if is_docstring:
                prev = tok.type
                continue
            if tok.type not in (tokenize.NL,):
                prev = tok.type
            pieces.append(tok.string)
    return " ".join(pieces)


def test_module_never_opens_the_log_for_writing():
    """An audit log that can be rewritten after a loss is not evidence."""
    src = _code_only(
        Path(__file__).resolve().parents[1] / "src" / "tp2agent" / "audit.py"
    )
    for mode in ('"w"', '"w+"', '"wb"', '"r+"', '"x"'):
        assert mode not in src, f"a truncating open ({mode}) must not appear in code"
    assert "truncate" not in src, "no truncation path"
    assert "unlink" not in src, "no deletion path"
    assert '"a"' in src, "the log is opened in append mode"


def test_reopening_continues_the_same_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        _log(p).append(_rec(episode_key="before"))
        _log(p).append(_rec(episode_key="after"))     # a fresh instance
        assert [r.episode_key for r in _log(p).read()] == ["before", "after"]


def test_records_are_immutable():
    r = _rec()
    try:
        r.outcome = Outcome.TRADED  # type: ignore[misc]
    except (FrozenInstanceError, AttributeError):
        return
    raise AssertionError("a record describes what already happened; it must not be editable")


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


def test_every_outcome_round_trips():
    with tempfile.TemporaryDirectory() as d:
        lg = _log(Path(d))
        for o in Outcome:
            lg.append(_rec(outcome=o, episode_key=o.value))
        assert [r.outcome for r in lg.read()] == list(Outcome)


def test_refusals_are_recorded_not_just_orders():
    """orders.jsonl is survivorship-biased; this file must show the declines."""
    with tempfile.TemporaryDirectory() as d:
        lg = _log(Path(d))
        lg.log(underlying="SPY", outcome=Outcome.RISK_REJECTED, reason="cap")
        lg.log(underlying="SPY", outcome=Outcome.ABSTAINED, reason="below threshold")
        lg.log(underlying="SPY", outcome=Outcome.TRADED, reason="sent")
        s = lg.summary()
        assert s == {"risk_rejected": 1, "abstained": 1, "traded": 1}


def test_risk_evidence_keeps_passes_as_well_as_failures():
    with tempfile.TemporaryDirectory() as d:
        lg = _log(Path(d))
        lg.append(_rec(risk={"approved": False,
                             "rejections": [{"code": "daily_stop", "message": "x"}],
                             "checks_passed": ["spread", "staleness"]}))
        got = lg.read()[0].risk
        assert got["checks_passed"] == ["spread", "staleness"]
        assert got["rejections"][0]["code"] == "daily_stop"


def test_decision_ids_are_unique():
    ids = {DecisionRecord(ts="t", underlying="SPY", outcome=Outcome.TRADED).decision_id
           for _ in range(200)}
    assert len(ids) == 200


def test_log_is_valid_jsonl():
    with tempfile.TemporaryDirectory() as d:
        lg = _log(Path(d))
        lg.append(_rec(quotes={"A": {"bid": 1.0, "ask": 1.1}}))
        for line in lg.path.read_text().splitlines():
            json.loads(line)


def test_a_malformed_line_does_not_hide_the_rest():
    with tempfile.TemporaryDirectory() as d:
        lg = _log(Path(d))
        lg.append(_rec(episode_key="good1"))
        with lg.path.open("a") as fh:
            fh.write("{not json\n")
        lg.append(_rec(episode_key="good2"))
        assert [r.episode_key for r in lg.read()] == ["good1", "good2"]


def test_is_action_marks_only_real_state_changes():
    assert Outcome.TRADED.is_action and Outcome.CLOSED.is_action
    for o in (Outcome.ABSTAINED, Outcome.RISK_REJECTED, Outcome.HELD,
              Outcome.THEORY_BLOCKED, Outcome.NOT_EXECUTABLE, Outcome.ORDER_FAILED):
        assert not o.is_action


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

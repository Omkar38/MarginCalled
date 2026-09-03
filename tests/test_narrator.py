"""Tests for narration, and for the boundary that keeps it harmless.

The most important tests here are not about prose quality. They are the two
directions of the one-way rule: the narrator cannot reach the decision path, and
the decision path cannot reach the narrator. Everything else the agent claims
about determinism rests on that.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.audit import AuditLog, DecisionRecord, Outcome  # noqa: E402
from tp2agent.narrator import (  # noqa: E402
    DEFAULT_MODEL,
    LLMNarrator,
    TemplateNarrator,
    narrate_record,
    narrate_session,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "tp2agent"

# Modules that decide or act. Nothing here may depend on narration, and
# narration may not depend on any of them.
DECISION_PATH = (
    "risk", "executor", "position", "exits", "rectangles", "theory_gate", "features",
)


def _imports(module: str) -> set[str]:
    tree = ast.parse((SRC / f"{module}.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
    return found



class _NoCredentials:
    """Context manager clearing the Anthropic credentials.

    LLMNarrator(api_key="") falls back to the environment, because "" is falsy.
    So a test asserting "no key" behaviour passes on a bare machine and fails
    the moment a real key is exported - which is exactly what happened once the
    key went into .env and the compliance report loaded it before running the
    suite. Same trap as the Alpaca credential test.
    """

    VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID")

    def __enter__(self):
        import os

        self.saved = {k: os.environ.pop(k, None) for k in self.VARS}
        return self

    def __exit__(self, *exc):
        import os

        for k, v in self.saved.items():
            if v is not None:
                os.environ[k] = v
        return False


def _rec(**kw) -> DecisionRecord:
    base = dict(
        ts="2026-09-02T10:00:00", underlying="SPY", episode_key="SPY261016C640",
        outcome=Outcome.RISK_REJECTED,
        determinant={"lhs": 54.0, "rhs": 56.2, "normalized_severity": 0.02},
        risk={"approved": False,
              "rejections": [{"code": "per_trade_cap", "message": "max loss 11940 exceeds 250"}],
              "checks_passed": ["spread", "staleness"]},
    )
    base.update(kw)
    return DecisionRecord(**base)


# --------------------------------------------------------------------------
# The one-way rule
# --------------------------------------------------------------------------


def test_narrator_does_not_import_the_decision_path():
    """Narration reads records. It must not be able to touch risk or execution."""
    imported = _imports("narrator")
    leaked = {m for m in DECISION_PATH if m in imported}
    assert not leaked, f"narrator must not import the decision path: {leaked}"


def test_the_decision_path_does_not_import_the_narrator():
    """The other direction. The first time a narration is consulted before a
    trade, the deterministic gates stop being deterministic."""
    for module in DECISION_PATH:
        assert "narrator" not in _imports(module), f"{module}.py must not import narrator"


def test_narration_returns_only_text():
    out = narrate_record(_rec())
    assert isinstance(out, str) and out


def test_narration_does_not_mutate_the_record():
    r = _rec()
    before = json.dumps(r.to_record(), sort_keys=True, default=str)
    TemplateNarrator().record(r)
    LLMNarrator(api_key="").record(r)
    assert json.dumps(r.to_record(), sort_keys=True, default=str) == before


def test_llm_narrator_is_given_no_tools():
    """A narrator with tools is a participant. The request body must carry none."""
    src = (SRC / "narrator.py").read_text()
    assert '"tools"' not in src and "'tools'" not in src
    assert "tool_use" not in src


def test_llm_system_prompt_forbids_inventing_facts():
    assert "ONLY facts present in the records" in LLMNarrator.SYSTEM
    assert "reader, not a participant" in LLMNarrator.SYSTEM


# --------------------------------------------------------------------------
# Fallback: a narration failure must never look like a trading failure
# --------------------------------------------------------------------------


def test_llm_falls_back_to_the_template_without_a_key():
    with _NoCredentials():
        n = LLMNarrator(api_key="")
        assert not n.available
        out = n.record(_rec())
        assert out == TemplateNarrator().record(_rec())
        assert n.last_error == "no ANTHROPIC_API_KEY set"


def test_llm_falls_back_when_the_call_fails():
    n = LLMNarrator(api_key="sk-test")
    n._call = lambda prompt: None          # simulate any transport failure
    assert n.record(_rec()) == TemplateNarrator().record(_rec())
    assert n.session([_rec()]) == TemplateNarrator().session([_rec()])


def test_default_model_is_current():
    assert DEFAULT_MODEL == "claude-opus-5"


# --------------------------------------------------------------------------
# What the narration actually says
# --------------------------------------------------------------------------


def test_refusal_names_the_gate_that_refused():
    out = TemplateNarrator().record(_rec())
    assert "per_trade_cap" in out
    assert "max loss 11940 exceeds 250" in out


def test_passed_gates_are_reported_too():
    """A refusal that hides what passed overstates how close the trade was."""
    assert "2 other gates passed" in TemplateNarrator().record(_rec())


def test_abstention_is_narrated_as_a_decision_not_a_gap():
    r = _rec(
        outcome=Outcome.ABSTAINED, risk={},
        selector={"name": "model", "scores": {"T1": 0.41, "K2": 0.38},
                  "reason": "best probability 0.4100 is below the 0.50 threshold"},
    )
    out = TemplateNarrator().record(r)
    assert "stood down" in out
    assert "T1 0.4100" in out and "K2 0.3800" in out
    assert "below the 0.50 threshold" in out


def test_traded_record_reports_the_limit_and_the_shading():
    r = _rec(outcome=Outcome.TRADED, risk={"approved": True, "checks_passed": ["a", "b"]},
             order={"limit_price": -1.25, "is_debit": False, "shade": 0.10,
                    "indicative_net": -1.15},
             broker={"status": "accepted", "order_id": "abc-123"})
    out = TemplateNarrator().record(r)
    assert "-1.25" in out and "credit" in out
    assert "accepted" in out and "abc-123" in out
    assert "all 2 risk gates passed" in out


def test_session_summary_counts_outcomes():
    records = [_rec(), _rec(outcome=Outcome.ABSTAINED, risk={}),
               _rec(outcome=Outcome.TRADED, risk={})]
    out = TemplateNarrator().session(records)
    assert "3 decisions recorded; 1 became orders." in out
    assert "abstained 1" in out and "risk_rejected 1" in out


def test_empty_session_says_so():
    assert TemplateNarrator().session([]) == "No decisions recorded."


def test_narrate_session_writes_beside_the_log_without_touching_it():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        lg = AuditLog(p / "decisions.jsonl")
        lg.append(_rec())
        before = lg.path.read_bytes()
        out = p / "narration.md"
        text = narrate_session(lg, out=out)
        assert out.read_text().startswith(text[:20])
        assert lg.path.read_bytes() == before, "narrating must not modify the evidence"


def test_narrate_cli_loads_dotenv():
    """narrate.py read ANTHROPIC_API_KEY from the environment but never loaded
    the file the key is documented to live in, so pasting the key where the
    comments say would have had no effect and the narrator would have gone on
    silently using its template."""
    src = (Path(__file__).resolve().parents[1] / "scripts" / "narrate.py").read_text()
    assert "load_env" in src, "the narrate CLI must load .env"


def test_load_env_does_not_override_the_environment():
    import os

    from tp2agent.env import load_env

    os.environ["TP2_TEST_SENTINEL"] = "explicit"
    try:
        load_env()
        assert os.environ["TP2_TEST_SENTINEL"] == "explicit"
    finally:
        os.environ.pop("TP2_TEST_SENTINEL", None)


# --------------------------------------------------------------------------
# A wrong key must be diagnosable, not merely survivable
# --------------------------------------------------------------------------


def test_check_reports_a_missing_key_clearly():
    with _NoCredentials():
        ok, detail = LLMNarrator(api_key="").check()
    assert not ok
    assert "ANTHROPIC_API_KEY" in detail


def test_http_error_bodies_are_parsed_into_the_message():
    """A bare 401 with a silent template fallback leaves the user with no idea
    the key is wrong. The API's own message has to survive."""
    import io
    import urllib.error

    n = LLMNarrator(api_key="sk-bad")
    body = json.dumps({"error": {"message": "API key is invalid."}}).encode()

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 401, "Unauthorized", {},
            io.BytesIO(body),
        )

    import tp2agent.narrator as mod

    saved = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = boom
    try:
        assert n._call("hi") is None
        assert "401" in n.last_error
        assert "API key is invalid." in n.last_error
        assert "check ANTHROPIC_API_KEY" in n.last_error
    finally:
        mod.urllib.request.urlopen = saved


def test_a_failed_call_still_produces_narration():
    """The fallback stays: a narration failure must never look like a trading
    failure. It just must not be silent about why."""
    n = LLMNarrator(api_key="sk-bad")
    n._call = lambda prompt: None
    out = n.record(_rec())
    assert out and isinstance(out, str)


def test_check_is_cheap():
    """check() must not spend a full narration's tokens proving a key works."""
    n = LLMNarrator(api_key="sk-x", max_tokens=4000)
    seen = {}

    def fake(prompt):
        seen["max_tokens"] = n.max_tokens
        return "ready"

    n._call = fake
    ok, _ = n.check()
    assert ok
    assert seen["max_tokens"] <= 32, "the probe must cap max_tokens"
    assert n.max_tokens == 4000, "and must restore the original setting"


def test_workspace_header_is_sent_only_when_configured():
    """Identity-linked keys are rejected without anthropic-workspace-id, and
    ordinary keys must not receive the header at all."""
    import io
    import urllib.error

    import tp2agent.narrator as mod

    captured = {}

    def fake(req, timeout=None):
        captured["headers"] = dict(req.headers)
        raise urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"{}"))

    saved = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = fake
    try:
        LLMNarrator(api_key="sk-x", workspace_id="")._call("hi")
        keys = {k.lower() for k in captured["headers"]}
        assert "anthropic-workspace-id" not in keys

        LLMNarrator(api_key="sk-x", workspace_id="wrkspc_123")._call("hi")
        hdrs = {k.lower(): v for k, v in captured["headers"].items()}
        assert hdrs.get("anthropic-workspace-id") == "wrkspc_123"
    finally:
        mod.urllib.request.urlopen = saved


def test_identity_linked_error_names_the_missing_variable():
    import io
    import urllib.error

    import tp2agent.narrator as mod

    body = json.dumps({"error": {"message":
        "anthropic-workspace-id is required when authenticating with an "
        "identity-linked API key"}}).encode()

    def fake(req, timeout=None):
        raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(body))

    saved = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = fake
    try:
        n = LLMNarrator(api_key="sk-x", workspace_id="")
        n._call("hi")
        assert "ANTHROPIC_WORKSPACE_ID" in n.last_error
    finally:
        mod.urllib.request.urlopen = saved


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

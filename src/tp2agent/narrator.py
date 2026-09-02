"""Turns decision records into a human-readable account of what the agent did.

THE ONE-WAY RULE
    Records flow in. Text flows out. Nothing else.

    This module must never influence a trade. It imports nothing from the
    decision path - not risk, not executor, not position, not exits - and no
    module in that path imports it. Both halves are asserted by tests, because
    the property is easy to state and easy to erode: the first time a narrator's
    output is used to skip a gate, the deterministic risk layer stops being
    deterministic and every guarantee built on it becomes a claim rather than a
    fact.

    So the language model here is a reader, not a participant. It is given no
    tools, no ability to place or cancel anything, and its output is written to
    a separate file that the trading path never opens. If it hallucinates
    outright, the trades that happened are still exactly the trades the
    deterministic gates approved - the narration is wrong, the book is not.

WHY NARRATE AT ALL
    The interesting behaviour of this agent is refusal. Most rectangles are
    detected and declined - by the theory gate, by a risk cap, by the selector
    standing down - and a log of orders cannot show any of that, because those
    candidates never became orders. Narration is how the reasoning becomes
    legible, and an agent that can say precisely why it did not trade is
    demonstrating more than one that only reports fills.

TWO NARRATORS
    `TemplateNarrator` is deterministic, needs no network and no key, and is
    the default. It always works, which matters when the thing being
    demonstrated is reliability.

    `LLMNarrator` calls Claude for prose that reads better across a whole
    session. It falls back to the template on any failure - missing key, network
    error, bad response - because a narration failure must never look like a
    trading failure.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .audit import AuditLog, DecisionRecord, Outcome

__all__ = [
    "TemplateNarrator",
    "LLMNarrator",
    "narrate_record",
    "narrate_session",
    "DEFAULT_MODEL",
]

DEFAULT_MODEL = "claude-opus-5"
API_URL = "https://api.anthropic.com/v1/messages"

_OUTCOME_PHRASE = {
    Outcome.THEORY_BLOCKED: "declined - the early-exercise gate could not certify the rectangle",
    Outcome.NOT_EXECUTABLE: "declined - not executable on the terms quoted",
    Outcome.ABSTAINED: "stood down - the selector would not choose a denomination",
    Outcome.RISK_REJECTED: "refused by the risk gates",
    Outcome.ORDER_FAILED: "attempted, and the submission failed",
    Outcome.TRADED: "traded",
    Outcome.HELD: "held",
    Outcome.CLOSED: "closed",
}


def _fmt(value, spec: str = ".4f") -> str:
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return "n/a"


class TemplateNarrator:
    """Deterministic narration. No network, no key, no failure mode."""

    name = "template"

    def record(self, r: DecisionRecord) -> str:
        phrase = _OUTCOME_PHRASE.get(r.outcome, r.outcome.value)
        # NOT_EXECUTABLE is reached from two different stages and they mean
        # different things: the execution screen rejecting the quoted terms is
        # not the same as no covered whole-contract ratio existing. Reporting
        # both with one phrase would misdescribe the more common case.
        if r.outcome is Outcome.NOT_EXECUTABLE:
            if r.stage == "tradability":
                phrase = "declined - the execution screen rejected the quoted terms"
            elif r.stage == "position":
                phrase = "declined - no covered whole-contract ratio exists"
        head = f"[{r.ts}] {r.underlying} {r.episode_key or '-'} - {phrase}"
        lines = [head]

        if r.determinant:
            d = r.determinant
            lines.append(
                f"    determinant: A*B ask {_fmt(d.get('lhs'))} vs C*D bid "
                f"{_fmt(d.get('rhs'))}, severity {_fmt(d.get('normalized_severity'), '.4%')}"
            )
        if r.theory_category:
            lines.append(f"    theory: {r.theory_category}")
        if r.selector:
            s = r.selector
            scores = s.get("scores") or {}
            if scores:
                shown = ", ".join(f"{k} {_fmt(v)}" for k, v in scores.items())
                lines.append(f"    selector ({s.get('name', '?')}): {shown}")
            if s.get("reason"):
                lines.append(f"    selector said: {s['reason']}")
        if r.denomination:
            lines.append(f"    denomination: {r.denomination}")

        risk = r.risk or {}
        rejections = risk.get("rejections") or []
        passed = risk.get("checks_passed") or []
        if rejections:
            for item in rejections:
                lines.append(f"    REFUSED {item.get('code')}: {item.get('message')}")
            if passed:
                lines.append(f"    ({len(passed)} other gates passed)")
        elif passed:
            lines.append(f"    all {len(passed)} risk gates passed")

        if r.order:
            o = r.order
            kind = "debit" if o.get("is_debit") else "credit"
            lines.append(
                f"    order: limit {_fmt(o.get('limit_price'), '+.2f')} ({kind}), "
                f"shaded {_fmt(o.get('shade'))} from an indicative "
                f"{_fmt(o.get('indicative_net'), '+.4f')}"
            )
        if r.broker:
            b = r.broker
            lines.append(f"    broker: {b.get('status', 'n/a')} {b.get('order_id') or ''}".rstrip())
        if r.reason and r.reason not in head:
            lines.append(f"    note: {r.reason}")
        return "\n".join(lines)

    def session(self, records: list[DecisionRecord]) -> str:
        if not records:
            return "No decisions recorded."
        counts: dict[str, int] = {}
        for r in records:
            counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        considered = len(records)
        traded = counts.get(Outcome.TRADED.value, 0)
        head = [
            f"{considered} decisions recorded; {traded} became orders.",
            "Outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
            "",
        ]
        return "\n".join(head + [self.record(r) for r in records])


class LLMNarrator:
    """Claude-backed narration. Reads records; can do nothing else.

    Given no tools and no credentials beyond its own API key, so there is no
    mechanism by which its output could reach the broker. Falls back to the
    template on any failure.
    """

    name = "llm"

    SYSTEM = (
        "You are the reporting layer of an autonomous options-trading agent. You are given "
        "structured records of decisions the agent has ALREADY made and you write a short, "
        "precise account of them for a human reviewer.\n\n"
        "You are a reader, not a participant. You do not decide anything, you do not "
        "recommend trades, and nothing you write is read back by the trading system.\n\n"
        "The agent trades TP2 violations in call options: it detects rectangles where "
        "C_bid*D_bid exceeds A_ask*B_ask, checks a no-early-exercise theory gate, sizes a "
        "covered position, and passes it through deterministic risk gates. Most candidates are "
        "refused. Refusals are the normal case and are worth explaining clearly.\n\n"
        "Rules:\n"
        "- Use ONLY facts present in the records. Never invent a number, a gate, or an outcome.\n"
        "- If something is absent, say it is absent rather than guessing.\n"
        "- Be concrete and brief. Prefer naming the specific gate that refused a trade.\n"
        "- Do not give trading advice or opinions about whether a decision was correct.\n"
        "- Plain prose. No preamble, no headings unless summarising a whole session."
    )

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 max_tokens: int = 1200, timeout: int = 60,
                 max_records: int = 120) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_records = max_records
        self.fallback = TemplateNarrator()
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _call(self, prompt: str) -> str | None:
        if not self.available:
            self.last_error = "no ANTHROPIC_API_KEY set"
            return None
        body = json.dumps({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            text = "".join(parts).strip()
            if not text:
                self.last_error = "empty response"
                return None
            self.last_error = None
            return text
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError,
                TimeoutError, OSError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def record(self, r: DecisionRecord) -> str:
        prompt = (
            "Write two or three sentences describing this single decision.\n\n"
            + json.dumps(r.to_record(), indent=2, default=str)
        )
        return self._call(prompt) or self.fallback.record(r)

    def session(self, records: list[DecisionRecord]) -> str:
        if not records:
            return "No decisions recorded."
        # A full session can hold thousands of records. Sending all of them is
        # slow and mostly redundant, since refusals repeat. Send the counts in
        # full and a bounded sample of the records themselves, and say plainly
        # that it is a sample so the model does not report the sample size as
        # the session size.
        counts: dict[str, int] = {}
        for r in records:
            counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        sample = records if len(records) <= self.max_records else (
            [r for r in records if r.outcome.is_action]
            + [r for r in records if not r.outcome.is_action][: self.max_records]
        )
        payload = {
            "total_decisions": len(records),
            "outcome_counts": counts,
            "records_shown": len(sample),
            "note": ("all records shown" if len(sample) == len(records)
                     else "a sample; use outcome_counts for totals"),
            "records": [r.to_record() for r in sample],
        }
        prompt = (
            "Summarise this trading session for a human reviewer. Say how many candidates were "
            "considered, how many were traded, and - most importantly - why the rest were not. "
            "Group the refusals by cause rather than listing them one by one. Finish with any "
            "pattern worth a human's attention.\n\n"
            + json.dumps(payload, indent=2, default=str)
        )
        out = self._call(prompt)
        if out is None:
            return self.fallback.session(records)
        return out


def narrate_record(record: DecisionRecord, narrator=None) -> str:
    return (narrator or TemplateNarrator()).record(record)


def narrate_session(log: AuditLog | Path | str, narrator=None, out: Path | str | None = None) -> str:
    """Narrate a whole audit log, optionally writing the prose beside it.

    The output path is deliberately a different file from the audit log: the log
    is evidence and stays append-only, the narration is a rendering of it and may
    be regenerated freely. Nothing reads the narration back.
    """
    audit = log if isinstance(log, AuditLog) else AuditLog(log)
    text = (narrator or TemplateNarrator()).session(audit.read())
    if out is not None:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
    return text

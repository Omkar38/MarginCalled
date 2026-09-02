#!/usr/bin/env python3
"""Turn a decision log into a readable account of what the agent did.

    python3 scripts/narrate.py --data-dir data/SPY
    python3 scripts/narrate.py --data-dir data/SPY --llm --out reports/SPY_narration.md

Reads decisions.jsonl and writes prose. It never writes to the log, and nothing
in the trading path reads what it produces.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tp2agent.audit import AuditLog  # noqa: E402
from tp2agent.narrator import DEFAULT_MODEL, LLMNarrator, TemplateNarrator  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/SPY", help="directory holding decisions.jsonl")
    ap.add_argument("--llm", action="store_true", help="use Claude instead of the template")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default=None, help="write the narration to this file as well")
    ap.add_argument("--last", type=int, default=0, help="narrate only the last N decisions")
    args = ap.parse_args()

    log = AuditLog(Path(args.data_dir) / "decisions.jsonl")
    records = log.read()
    if not records:
        print(f"no decisions recorded in {log.path}")
        return 1
    if args.last:
        records = records[-args.last:]

    narrator = LLMNarrator(model=args.model) if args.llm else TemplateNarrator()
    if args.llm and not narrator.available:
        print("  ANTHROPIC_API_KEY is not set; falling back to the template narrator\n",
              file=sys.stderr)

    text = narrator.session(records)
    if args.llm and getattr(narrator, "last_error", None):
        print(f"  (LLM unavailable: {narrator.last_error}; used the template)\n",
              file=sys.stderr)

    print(text)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
        print(f"\n  written to {p}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

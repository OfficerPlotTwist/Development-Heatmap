#!/usr/bin/env python3
"""Generate 3-line LLM descriptions of each session, cached by session id.

Reads digests.json (produced by scan_sessions.py), writes summaries.json
({session_id: "<=3 line description"}). Only summarizes sessions missing from
the cache, so re-runs are cheap. After running, re-run scan_sessions.py to merge
the new descriptions into data.json / the HTML.

HARNESS DEPENDENCY: needs the `anthropic` package and an ANTHROPIC_API_KEY in the
environment — neither ships with a bare codex / claude-code install. If you can't
provide a key, the project's AGENTS.md documents the subagent fallback that fills
summaries.json without the API. See AGENTS.md.

Usage:
  python summarize.py            # fill missing descriptions
  python summarize.py --force    # re-summarize everything
  python summarize.py --limit 20 # cap how many to do this run
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIGESTS = HERE / "digests.json"
SUMMARIES = HERE / "summaries.json"
MODEL = "claude-haiku-4-5-20251001"  # cheap + fast; fine for short summaries

SYS = (
    "You summarize a single AI coding/work session for a dashboard. "
    "Given the user's asks, the tools used, and the assistant's final message, "
    "write a description of WHAT THE USER WANTED and WHAT WAS ACCOMPLISHED. "
    "Be concrete and specific (name the artifact, tool, or outcome). "
    "Hard limit: 3 lines, no markdown, no preamble, no trailing period spam."
)


def build_prompt(d: dict) -> str:
    asks = "\n".join(f"- {p}" for p in d.get("prompts", [])) or "(none captured)"
    tools = ", ".join(d.get("tools", [])) or "(none)"
    final = d.get("final", "") or "(no final message)"
    return (
        f"Directory: {d.get('path')}\n"
        f"Date: {d.get('date')}\n\n"
        f"User asks:\n{asks}\n\n"
        f"Tools used: {tools}\n\n"
        f"Assistant's final message (truncated):\n{final}\n\n"
        "Write the 3-line-max description now:"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not DIGESTS.exists():
        sys.exit("digests.json missing — run scan_sessions.py first.")
    digests = json.loads(DIGESTS.read_text(encoding="utf-8"))

    summaries = {}
    if SUMMARIES.exists():
        try:
            summaries = json.loads(SUMMARIES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    todo = [sid for sid in digests if args.force or not summaries.get(sid)]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing to summarize — cache is complete.")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY not set. Either export a key, or use the subagent "
            "fallback described in AGENTS.md to populate summaries.json."
        )
    try:
        import anthropic
    except ImportError:
        sys.exit("`anthropic` not installed. Run: pip install anthropic")

    client = anthropic.Anthropic()
    print(f"Summarizing {len(todo)} sessions with {MODEL} …")
    for i, sid in enumerate(todo, 1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=180,
                system=SYS,
                messages=[{"role": "user", "content": build_prompt(digests[sid])}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            # enforce 3 lines
            summaries[sid] = "\n".join(text.splitlines()[:3]).strip()
        except Exception as e:  # noqa: BLE001 - keep going, save partial work
            print(f"  ! {sid[:8]} failed: {e}", file=sys.stderr)
            continue
        if i % 10 == 0 or i == len(todo):
            SUMMARIES.write_text(json.dumps(summaries, indent=None), encoding="utf-8")
            print(f"  {i}/{len(todo)} done")

    SUMMARIES.write_text(json.dumps(summaries, indent=None), encoding="utf-8")
    print(f"Wrote {SUMMARIES}. Re-run scan_sessions.py to merge into the viewer.")


if __name__ == "__main__":
    main()

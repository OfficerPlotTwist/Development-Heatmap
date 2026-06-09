#!/usr/bin/env python3
"""Assay the kindling backlog with a model.

For every kindling session (limit-hit-unresumed or waiting-for-input), this gathers
the CURRENT state of its directory (recent git commits, file tree, newer sessions in
the same dir) and asks a model whether resuming the session is still USEFUL given how
the directory has moved on. Verdict per session: useful | stale | obsolete, with a
one-line reason. Results -> kindling_assay.json, which scan_sessions.py merges in.

Two ways to run it (mirrors summarize.py):
  A) API path  — needs `anthropic` + ANTHROPIC_API_KEY:
       python assay_kindling.py           # gathers context, evaluates, writes verdicts
  B) Subagent path — no key:
       python assay_kindling.py --emit     # writes kindling_assay_input.json only
     then fan out subagents over the input slices (see AGENTS.md), merge their
     {session_id: {verdict, reason}} into kindling_assay.json.

Then re-run scan_sessions.py to surface verdicts in the viewer.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data.json"
INPUT = HERE / "kindling_assay_input.json"   # model-ready context per session
OUT = HERE / "kindling_assay.json"           # session_id -> {verdict, reason, ...}
MODEL = "claude-sonnet-4-6"                   # judgment task -> reasoning-tier default

VERDICTS = ("useful", "stale", "obsolete")
SYS = (
    "You assay an ABANDONED dev session against the CURRENT state of its directory and "
    "decide whether resuming it is still worth doing. You are given the session's intent "
    "and how the directory has moved on since (recent commits, files, newer sessions).\n"
    "Reply with ONLY a JSON object: {\"verdict\": one of useful|stale|obsolete, "
    "\"reason\": \"<=20 words citing the evidence\"}.\n"
    "useful  = the need is real and NOT yet addressed; resuming is worth it.\n"
    "stale   = partially overtaken or uncertain; may need a quick re-check before resuming.\n"
    "obsolete= already done, superseded, or no longer relevant given the current state."
)


def git(d: Path, *args):
    try:
        r = subprocess.run(["git", "-C", str(d), *args],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def gather(s, sessions, root_paths):
    root = root_paths.get(s["segs"][0] if s["segs"] else "", "")
    sub = "/".join(s["segs"][1:])
    cwd = Path(root + ("/" + sub if sub else "")) if root else None
    key = "/".join(s["segs"])
    newer = sum(1 for x in sessions
                if "/".join(x["segs"]) == key and (x.get("ts") or "") > (s.get("ts") or ""))
    since = (s.get("ts") or "")[:10]
    repo = bool(cwd and cwd.exists() and git(cwd, "rev-parse", "--is-inside-work-tree") == "true")
    commits, recent = 0, []
    if repo:
        log = git(cwd, "log", f"--since={since}", "--pretty=%cs %s", "--", ".") or ""
        recent = [l for l in log.splitlines() if l.strip()][:15]
        commits = len(recent)
    files = []
    if cwd and cwd.exists():
        try:
            files = sorted(p.name + ("/" if p.is_dir() else "") for p in cwd.iterdir())[:40]
        except OSError:
            pass
    return {
        "session": s["session"], "dir": key, "date": s.get("date"),
        "wreason": "limit" if s.get("limit") else (s.get("wreason") or "waiting"),
        "intent": (s.get("desc") or s.get("title") or "").strip(),
        "is_git": repo, "commits_since": commits, "newer_sessions": newer,
        "recent_commits": recent, "files": files,
    }


def build_prompt(c):
    return (
        f"SESSION INTENT: {c['intent'] or '(unknown)'}\n"
        f"why it stalled: {c['wreason']}   stalled on: {c['date']}\n"
        f"directory: {c['dir']}\n\n"
        f"CURRENT DIRECTORY STATE\n"
        f"- git repo: {c['is_git']}; commits since it stalled: {c['commits_since']}\n"
        f"- newer sessions in this exact dir since: {c['newer_sessions']}\n"
        f"- recent commits:\n" + ("\n".join(f"    {l}" for l in c['recent_commits']) or "    (none)") + "\n"
        f"- files now:\n    " + (", ".join(c['files']) or "(none)") + "\n\n"
        "Assay it. JSON only."
    )


def load(p):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true", help="only write the model-ready input file")
    ap.add_argument("--force", action="store_true", help="re-assay everything")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    if not DATA.exists():
        sys.exit("data.json missing — run scan_sessions.py first.")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    sessions = data["sessions"]
    root_paths = data.get("root_paths", {})
    kindling = [s for s in sessions
                if (s.get("limit") and not s.get("resumed")) or s.get("waiting")]

    print(f"Gathering directory state for {len(kindling)} kindling sessions…")
    ctx = {c["session"]: c for c in (gather(s, sessions, root_paths) for s in kindling)}
    INPUT.write_text(json.dumps(ctx, indent=None), encoding="utf-8")
    print(f"Wrote {INPUT}")

    if args.emit:
        print("Emitted context only. Run the subagent path (see AGENTS.md) or drop --emit with a key.")
        return

    out = load(OUT)
    todo = [sid for sid in ctx if args.force or not out.get(sid)]
    if not todo:
        print("Nothing to assay — cache complete.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set. Use the subagent path: "
                 "fan out over kindling_assay_input.json (see AGENTS.md), or export a key.")
    try:
        import anthropic
    except ImportError:
        sys.exit("`anthropic` not installed: pip install anthropic")

    client = anthropic.Anthropic()
    print(f"Assaying {len(todo)} sessions with {args.model}…")
    for i, sid in enumerate(todo, 1):
        try:
            resp = client.messages.create(
                model=args.model, max_tokens=120, system=SYS,
                messages=[{"role": "user", "content": build_prompt(ctx[sid])}])
            txt = "".join(b.text for b in resp.content if b.type == "text").strip()
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
            v = json.loads(txt)
            verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "stale"
            out[sid] = {"verdict": verdict, "reason": str(v.get("reason", ""))[:160],
                        "commits_since": ctx[sid]["commits_since"],
                        "newer_sessions": ctx[sid]["newer_sessions"]}
        except Exception as e:  # noqa: BLE001
            print(f"  ! {sid[:8]} failed: {e}", file=sys.stderr)
            continue
        if i % 10 == 0 or i == len(todo):
            OUT.write_text(json.dumps(out, indent=None), encoding="utf-8")
            print(f"  {i}/{len(todo)}")
    OUT.write_text(json.dumps(out, indent=None), encoding="utf-8")
    print(f"Wrote {OUT}. Re-run scan_sessions.py to merge verdicts.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Triage the kindling backlog against dev history.

For every kindling session (limit-hit-unresumed or waiting-for-input) this parses
the directory's git history and counts newer sessions in the same dir, then writes
a staleness verdict to kindling_triage.json. The viewer merges it in (re-run
scan_sessions.py) and shows it per item: a session whose repo moved on — commits
landed, or newer sessions ran — is likely out of date with the current repo state.

This is the "spin up triage" action: it identifies kindling that was ignored and
has since drifted from the repo. Deterministic (git + session timeline) — no API
key needed. Run, then re-run scan_sessions.py to surface verdicts.

Usage:
  python triage_kindling.py            # triage all kindling
  python triage_kindling.py --session <id>
"""
import argparse
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data.json"
OUT = HERE / "kindling_triage.json"


def git(dir_path: Path, *args):
    try:
        r = subprocess.run(["git", "-C", str(dir_path), *args],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def is_repo(dir_path: Path) -> bool:
    return git(dir_path, "rev-parse", "--is-inside-work-tree") == "true"


def triage_one(s, all_sessions):
    # reconstruct the working dir from root_paths + segs
    root_label = s["segs"][0] if s["segs"] else None
    root = ROOT_PATHS.get(root_label, "")
    sub = "/".join(s["segs"][1:])
    cwd = Path(root + ("/" + sub if sub else "")) if root else None

    key = "/".join(s["segs"])
    newer = sum(1 for x in all_sessions
                if "/".join(x["segs"]) == key and (x.get("ts") or "") > (s.get("ts") or ""))

    since = (s.get("ts") or "")[:10]
    commits_since = None
    last_commit = None
    repo = bool(cwd and cwd.exists() and is_repo(cwd))
    if repo:
        out = git(cwd, "log", f"--since={since}", "--pretty=%h", "--", ".")
        commits_since = len([l for l in (out or "").splitlines() if l.strip()])
        last_commit = git(cwd, "log", "-1", "--pretty=%cs %s", "--", ".")

    # verdict: out of date if the repo or session timeline moved on after it
    stale = (newer > 0) or bool(commits_since)
    bits = []
    if commits_since:
        bits.append(f"{commits_since} commit{'s' if commits_since != 1 else ''} since")
    if newer:
        bits.append(f"{newer} newer session{'s' if newer != 1 else ''}")
    if not repo and cwd and not cwd.exists():
        bits.append("dir gone")
    elif not repo:
        bits.append("no git repo")
    if stale:
        label = "stale · " + ", ".join(b for b in bits if "no git" not in b and "gone" not in b)
    else:
        label = "fresh" + (" · still pending" if repo else "")
    return {
        "stale": stale,
        "label": label.strip(" ·") or ("stale" if stale else "fresh"),
        "detail": "; ".join(bits) + (f"; last commit {last_commit}" if last_commit else ""),
        "repo": repo, "commits_since": commits_since, "newer_sessions": newer,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="triage only this session id")
    args = ap.parse_args()

    if not DATA.exists():
        raise SystemExit("data.json missing — run scan_sessions.py first.")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    global ROOT_PATHS
    ROOT_PATHS = data.get("root_paths", {})
    sessions = data["sessions"]
    kindling = [s for s in sessions
                if (s.get("limit") and not s.get("resumed")) or s.get("waiting")]
    if args.session:
        kindling = [s for s in kindling if s["session"] == args.session]

    out = {}
    if OUT.exists():
        try:
            out = json.loads(OUT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out = {}

    print(f"Triaging {len(kindling)} kindling sessions against git history…")
    stale = 0
    for i, s in enumerate(kindling, 1):
        v = triage_one(s, sessions)
        out[s["session"]] = v
        stale += 1 if v["stale"] else 0
        if i % 20 == 0 or i == len(kindling):
            print(f"  {i}/{len(kindling)}")
    OUT.write_text(json.dumps(out, indent=None), encoding="utf-8")
    print(f"Wrote {OUT}: {stale} stale / {len(kindling)} triaged.")
    print("Re-run scan_sessions.py to merge verdicts into the viewer.")


if __name__ == "__main__":
    main()

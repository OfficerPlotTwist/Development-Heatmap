#!/usr/bin/env python3
"""Scan Claude Code session logs and emit per-session metrics for the AI-folder
heatmap viewer.

For every session .jsonl under ~/.claude/projects, we pull:
  - cwd            (the real project dir, read from event lines, NOT the hash)
  - start date     (earliest timestamp in the session)
  - tokens         (sum of all usage fields across assistant messages)
  - prompts        (count of genuine user prompts, excluding tool-results/meta)

Only sessions whose cwd lives under the AI root are kept. Output is a single
self-contained data.json the HTML viewer reads. Path handling is portable:
the AI root and projects dir are derived from the home directory at runtime.
"""
import json
import re
import sys
from pathlib import Path

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
AI_ROOT = HOME / "Documents" / "AI"
HERE = Path(__file__).resolve().parent
OUT = HERE / "data.json"
DIGESTS = HERE / "digests.json"      # per-session gist -> input for summarize.py / subagents
SUMMARIES = HERE / "summaries.json"  # session_id -> LLM 3-line description (cache)
CACHE = HERE / "scan_cache.json"     # file -> {mtime,size,rec}: skip re-parsing unchanged logs
TEMPLATE = HERE / "template.html"
PAGE = HERE / "session_heatmap.html"


def norm(p: str) -> str:
    """Lowercase, forward-slash form for prefix comparison."""
    return p.replace("\\", "/").rstrip("/").lower()


AI_ROOT_NORM = norm(str(AI_ROOT))


def prompt_text(d: dict):
    """Return the text of a genuine user prompt, else None (tool-results/meta -> None)."""
    if d.get("type") != "user":
        return None
    if d.get("isMeta") or d.get("isCompactSummary"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        t = content.strip()
        return t or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = (block.get("text") or "").strip()
                if t:
                    return t
        return None
    return None


def sum_usage(usage: dict) -> int:
    if not isinstance(usage, dict):
        return 0
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    return sum(int(usage.get(k) or 0) for k in keys)


def clean_prompt(text: str) -> str:
    """Turn the raw first-prompt text into a readable title.

    Slash-command kickoffs arrive wrapped in <command-name>/<command-args> XML;
    pull those out. Otherwise strip any stray wrapper tags.
    """
    name = re.search(r"<command-name>([^<]+)</command-name>", text)
    if name:
        title = name.group(1).strip()
        args = re.search(r"<command-args>([^<]*)</command-args>", text)
        if args and args.group(1).strip():
            title += " " + args.group(1).strip()
        return title
    text = re.sub(r"<command-message>.*?</command-message>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)  # drop any stray tags
    return text


def clip(text: str, n: int = 160) -> str:
    text = " ".join(clean_prompt(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def assistant_text(msg: dict):
    """Concatenated text of an assistant message, plus tool_use names."""
    content = msg.get("content")
    txt, tools = "", []
    if isinstance(content, str):
        txt = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif b.get("type") == "tool_use" and b.get("name"):
                tools.append(b["name"])
        txt = "\n".join(parts)
    return txt.strip(), tools


def scan_file(path: Path):
    cwd = None
    first_ts = None
    last_ts = None
    tokens = 0
    prompts = 0
    title = None
    user_asks = []          # cleaned user prompts (for digest)
    final_asst = ""         # text of the most recent assistant message
    tools = set()
    seen = False
    limit_seen = False      # session hit the Claude session limit
    limit_reset = None      # the "resets <time>" string from the limit message
    events_after = None     # meaningful events since the last limit marker (None=no marker yet)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen = True
            if cwd is None and d.get("cwd"):
                cwd = d["cwd"]
            ts = d.get("timestamp")
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            is_marker = False
            if d.get("type") == "assistant":
                msg = d.get("message") or {}
                tokens += sum_usage(msg.get("usage"))
                atxt, atools = assistant_text(msg)
                tools.update(atools)
                if atxt:
                    final_asst = atxt
                # genuine session-limit hit (distinct from prompt-too-long / not-logged-in)
                if d.get("isApiErrorMessage") and "session limit" in atxt.lower():
                    is_marker = True
                    limit_seen = True
                    mm = re.search(r"resets\s+([^\n]+)", atxt)
                    limit_reset = mm.group(1).strip() if mm else atxt.strip()
                    events_after = 0
            # count real activity AFTER a limit marker -> was the session resumed?
            if (not is_marker and events_after is not None
                    and d.get("type") in ("user", "assistant") and not d.get("isMeta")):
                events_after += 1
            pt = prompt_text(d)
            if pt is not None:
                prompts += 1
                if title is None:
                    title = clip(pt)
                user_asks.append(clip(pt, 220))
    if not seen or cwd is None:
        return None

    # bounded digest: keep the arc (first asks + last couple) without bloating
    if len(user_asks) > 12:
        user_asks = user_asks[:10] + ["…"] + user_asks[-2:]
    digest = {
        "prompts": user_asks,
        "final": clip(final_asst, 900) if final_asst else "",
        "tools": sorted(tools)[:18],
    }
    return {"cwd": cwd, "ts": first_ts, "end": last_ts, "tokens": tokens,
            "prompts": prompts, "title": title, "session": path.stem,
            "file": str(path).replace("\\", "/"), "digest": digest,
            "limit": limit_seen, "reset": limit_reset,
            "resumed": bool((events_after or 0) > 0) if limit_seen else False}


def main():
    if not PROJECTS_DIR.exists():
        print(f"No projects dir at {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    # --- scan cache: skip re-parsing files whose (mtime,size) are unchanged.
    # This is the persistent table that minimizes future scan time: only new or
    # appended (e.g. resumed) session files get re-read. Stores the raw scan_file
    # record; per-run derivations (date/segs) are recomputed cheaply below.
    cache = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    new_cache = {}
    reused = parsed = 0

    sessions = []
    skipped = 0
    for jf in PROJECTS_DIR.glob("*/*.jsonl"):
        key = str(jf)
        try:
            st = jf.stat()
        except OSError:
            continue
        ent = cache.get(key)
        if ent and ent.get("mtime") == st.st_mtime and ent.get("size") == st.st_size:
            rec = ent.get("rec"); reused += 1
        else:
            rec = scan_file(jf); parsed += 1
        new_cache[key] = {"mtime": st.st_mtime, "size": st.st_size, "rec": rec}

        if rec is None:
            skipped += 1
            continue
        if not norm(rec["cwd"]).startswith(AI_ROOT_NORM):
            continue
        if not rec.get("ts"):
            continue
        rec = dict(rec)  # copy before adding per-run fields (don't mutate cache)
        rec["date"] = rec["ts"][:10]  # YYYY-MM-DD
        rel = norm(rec["cwd"])[len(AI_ROOT_NORM):].strip("/")
        rec["segs"] = [s for s in rel.split("/") if s] if rel else []
        sessions.append(rec)

    CACHE.write_text(json.dumps(new_cache), encoding="utf-8")
    sessions.sort(key=lambda r: r["ts"])
    kindling = [s for s in sessions if s.get("limit") and not s.get("resumed")]

    # --- digests (summarizer input), keyed by session id ---
    digests = {
        s["session"]: {
            "path": "/".join(s["segs"]) or "(AI root)",
            "date": s["date"],
            **s["digest"],
        }
        for s in sessions
    }
    DIGESTS.write_text(json.dumps(digests, indent=None), encoding="utf-8")

    # --- summaries cache (LLM 3-line descriptions); merged in if present ---
    summaries = {}
    if SUMMARIES.exists():
        try:
            summaries = json.loads(SUMMARIES.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summaries = {}
    have = sum(1 for s in sessions if summaries.get(s["session"]))

    dates = [s["date"] for s in sessions]
    payload = {
        "ai_root": str(AI_ROOT).replace("\\", "/"),
        "generated_from": str(PROJECTS_DIR).replace("\\", "/"),
        "count": len(sessions),
        "min_date": dates[0] if dates else None,
        "max_date": dates[-1] if dates else None,
        "sessions": [
            {"segs": s["segs"], "date": s["date"], "ts": s["ts"], "end": s["end"],
             "tokens": s["tokens"], "prompts": s["prompts"],
             "title": s["title"], "session": s["session"], "file": s["file"],
             "desc": summaries.get(s["session"]),
             "limit": s.get("limit", False), "reset": s.get("reset"),
             "resumed": s.get("resumed", False)}
            for s in sessions
        ],
    }
    data_str = json.dumps(payload, indent=None)
    OUT.write_text(data_str, encoding="utf-8")
    print(f"Sessions kept: {len(sessions)}  skipped(no cwd/empty): {skipped}")
    print(f"Scan cache: {reused} reused, {parsed} parsed")
    print(f"Date range: {payload['min_date']} -> {payload['max_date']}")
    print(f"Descriptions: {have}/{len(sessions)} present "
          f"({len(sessions) - have} need summarize.py / subagent pass)")
    print(f"Kindling (limit-hit, unresumed): {len(kindling)}")
    print(f"Wrote {OUT} and {DIGESTS}")

    # Embed data into the template -> single self-contained, double-clickable page.
    if TEMPLATE.exists():
        html = TEMPLATE.read_text(encoding="utf-8")
        # guard the closing </script> so embedded JSON can't break out
        safe = data_str.replace("</", "<\\/")
        html = html.replace("/*__DATA__*/", safe)
        PAGE.write_text(html, encoding="utf-8")
        print(f"Wrote {PAGE}")
    else:
        print(f"(template not found at {TEMPLATE}; skipped HTML build)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Scan Claude Code session logs and emit per-session metrics for the heatmap viewer.

For every session .jsonl under ~/.claude/projects, we pull:
  - cwd            (the real project dir, read from event lines, NOT the hash)
  - start date     (earliest timestamp in the session)
  - tokens         (sum of all usage fields across assistant messages)
  - prompts        (count of genuine user prompts, excluding tool-results/meta)

Sessions whose cwd lives under any configured ROOT directory are kept (config.json).
On first run, roots are auto-detected from your session history. Manage them with:
  python scan_sessions.py --list-roots
  python scan_sessions.py --add-root "C:/path/to/dir"
  python scan_sessions.py --remove-root "C:/path/to/dir"

Output is a single self-contained data.json the HTML viewer reads. Paths derive
from the home directory at runtime, so it's portable.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
OUT = HERE / "data.json"
DIGESTS = HERE / "digests.json"      # per-session gist -> input for summarize.py / subagents
SUMMARIES = HERE / "summaries.json"  # session_id -> LLM 3-line description (cache)
ASSAY = HERE / "kindling_assay.json"  # session_id -> model verdict on whether it's still useful
CACHE = HERE / "scan_cache.json"     # file -> {mtime,size,rec}: skip re-parsing unchanged logs
CONFIG = HERE / "config.json"        # {"roots": [...]} working dirs shown in the tool
TEMPLATE = HERE / "template.html"
PAGE = HERE / "session_heatmap.html"

# dirs that are never "working directories" even if sessions ran there
_EXCLUDE = ("/.claude", "/appdata/", "/.codex", "/.codewhale", "/npm-cache",
            "/.cache/", "/temp/", "/tmp/")


def norm(p: str) -> str:
    """Lowercase, forward-slash form for prefix comparison."""
    return p.replace("\\", "/").rstrip("/").lower()


# ---- config: which working directories the tool shows -----------------------
def load_config() -> dict:
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(cfg.get("roots"), list) and cfg["roots"]:
                return cfg
        except json.JSONDecodeError:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def autodetect_roots(cwds, top=3):
    """Identify working directories from session history: rank the 2-deep dirs
    under HOME by session count, excluding tool/cache/temp dirs. Used on first run."""
    home = norm(str(HOME))
    counts = Counter()
    for cwd in cwds:
        n = norm(cwd)
        if any(x in n for x in _EXCLUDE):
            continue
        if not n.startswith(home):
            continue
        rel = n[len(home):].strip("/").split("/")
        if not rel or not rel[0]:
            continue
        cand = "/".join([str(HOME).replace("\\", "/")] + rel[:2])
        counts[cand] += 1
    return [c for c, _ in counts.most_common(top)], counts


def root_label(root_path: str, all_roots) -> str:
    """Short label for a root; disambiguate if two roots share a basename."""
    base = Path(root_path).name
    if [Path(p).name for p in all_roots].count(base) > 1:
        return "/".join(Path(root_path).parts[-2:])
    return base


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
    last_meaningful = None  # type of the last real user/assistant turn
    open_tools = set()      # assistant tool_use ids with no matching tool_result yet
    pending_q = False       # an AskUserQuestion/ExitPlanMode awaiting the user
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
            typ = d.get("type")
            if cwd is None and d.get("cwd"):
                cwd = d["cwd"]
            ts = d.get("timestamp")
            if ts:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
            if typ in ("user", "assistant") and not d.get("isMeta"):
                last_meaningful = typ
            is_marker = False
            if typ == "assistant":
                msg = d.get("message") or {}
                tokens += sum_usage(msg.get("usage"))
                atxt, atools = assistant_text(msg)
                tools.update(atools)
                if atxt:
                    final_asst = atxt
                # track unfinished tool calls + pending user-questions
                for b in (msg.get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        if b.get("id"):
                            open_tools.add(b["id"])
                        if b.get("name") in ("AskUserQuestion", "ExitPlanMode"):
                            pending_q = True
                # genuine session-limit hit (distinct from prompt-too-long / not-logged-in)
                if d.get("isApiErrorMessage") and "session limit" in atxt.lower():
                    is_marker = True
                    limit_seen = True
                    mm = re.search(r"resets\s+([^\n]+)", atxt)
                    limit_reset = mm.group(1).strip() if mm else atxt.strip()
                    events_after = 0
            elif typ == "user":
                # tool_results close open tool calls
                uc = (d.get("message") or {}).get("content")
                if isinstance(uc, list):
                    for b in uc:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                            open_tools.discard(b["tool_use_id"])
            # count real activity AFTER a limit marker -> was the session resumed?
            if (not is_marker and events_after is not None
                    and typ in ("user", "assistant") and not d.get("isMeta")):
                events_after += 1
            pt = prompt_text(d)
            if pt is not None:
                prompts += 1
                pending_q = False  # the user answered
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
    # "waiting for input": ball is in the user's court and the chat was left open.
    # 'question' (assistant asked / AskUserQuestion pending) is the high-signal case;
    # 'interrupted' (a tool call never got its result) is noisier but still resumable.
    ends_q = final_asst.rstrip().endswith("?") if final_asst else False
    wreason = None
    if last_meaningful == "assistant" and not limit_seen:
        if pending_q or ends_q:
            wreason = "question"
        elif open_tools:
            wreason = "interrupted"
    return {"cwd": cwd, "ts": first_ts, "end": last_ts, "tokens": tokens,
            "prompts": prompts, "title": title, "session": path.stem,
            "file": str(path).replace("\\", "/"), "digest": digest,
            "limit": limit_seen, "reset": limit_reset,
            "resumed": bool((events_after or 0) > 0) if limit_seen else False,
            "waiting": wreason is not None, "wreason": wreason}


CACHE_VERSION = 3  # bump when scan_file's record schema changes (auto-invalidates cache)


def collect_records():
    """Scan every session log once (using the (mtime,size) cache) and return all
    non-empty records. This is the persistent table that minimizes future scan
    time — only new or appended (e.g. resumed) logs are re-read."""
    cache = {}
    if CACHE.exists():
        try:
            raw = json.loads(CACHE.read_text(encoding="utf-8"))
            if raw.get("_v") == CACHE_VERSION:
                cache = raw.get("files", {})
        except (json.JSONDecodeError, AttributeError):
            cache = {}
    new_cache = {}
    reused = parsed = skipped = 0
    recs = []
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
        else:
            recs.append(rec)
    CACHE.write_text(json.dumps({"_v": CACHE_VERSION, "files": new_cache}), encoding="utf-8")
    return recs, reused, parsed, skipped


def main():
    ap = argparse.ArgumentParser(description="Build the Development Heatmap from session logs.")
    ap.add_argument("--list-roots", action="store_true", help="show configured working dirs and exit")
    ap.add_argument("--add-root", action="append", metavar="DIR", help="add a working dir to include")
    ap.add_argument("--remove-root", action="append", metavar="DIR", help="stop including a working dir")
    args = ap.parse_args()

    if not PROJECTS_DIR.exists():
        sys.exit(f"No projects dir at {PROJECTS_DIR}")

    cfg = load_config()
    roots = list(cfg.get("roots", [])) if cfg else []

    if args.list_roots:
        print("Configured roots:" if roots else "No roots configured (auto-detected on next scan).")
        for r in roots:
            print("  ", r)
        return

    if args.add_root or args.remove_root:
        for r in (args.add_root or []):
            rp = str(Path(r).expanduser()).replace("\\", "/")
            if not any(norm(rp) == norm(x) for x in roots):
                roots.append(rp); print(f"+ added root: {rp}")
        for r in (args.remove_root or []):
            rp = str(Path(r).expanduser()).replace("\\", "/")
            roots = [x for x in roots if norm(x) != norm(rp)]
            print(f"- removed root: {rp}")
        save_config({"roots": roots})

    recs, reused, parsed, skipped = collect_records()

    # First run (or empty config): identify working directories from the history.
    if not roots:
        roots, counts = autodetect_roots([r["cwd"] for r in recs])
        if not roots:
            sys.exit("Could not auto-detect any working dirs. Add one: "
                     "python scan_sessions.py --add-root \"C:/path/to/dir\"")
        save_config({"roots": roots})
        print("First run — identified your working directories (edit config.json or use --add-root/--remove-root):")
        for r in roots:
            print(f"   {r}  ({counts[r]} sessions)")

    # longest-prefix match so nested roots resolve to the most specific one
    roots_norm = sorted(((norm(r), r) for r in roots), key=lambda t: -len(t[0]))
    labels = {r: root_label(r, roots) for r in roots}

    sessions = []
    for rec in recs:
        cwdn = norm(rec["cwd"])
        match = next((orig for rn, orig in roots_norm
                      if cwdn == rn or cwdn.startswith(rn + "/")), None)
        if not match or not rec.get("ts"):
            continue
        rec = dict(rec)  # copy before adding per-run fields (don't mutate cache)
        rec["date"] = rec["ts"][:10]
        rel = cwdn[len(norm(match)):].strip("/")
        sub = [s for s in rel.split("/") if s] if rel else []
        rec["segs"] = [labels[match]] + sub
        sessions.append(rec)

    sessions.sort(key=lambda r: r["ts"])
    kindling = [s for s in sessions
                if (s.get("limit") and not s.get("resumed")) or s.get("waiting")]

    # --- digests (summarizer input), keyed by session id ---
    digests = {
        s["session"]: {"path": "/".join(s["segs"]) or "(root)",
                       "date": s["date"], **s["digest"]}
        for s in sessions
    }
    DIGESTS.write_text(json.dumps(digests, indent=None), encoding="utf-8")

    # --- caches merged in if present: descriptions + kindling triage verdicts ---
    def load_json(p):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}
    summaries = load_json(SUMMARIES)
    assay = load_json(ASSAY)
    have = sum(1 for s in sessions if summaries.get(s["session"]))

    dates = [s["date"] for s in sessions]
    payload = {
        "roots": [r.replace("\\", "/") for r in roots],
        "root_paths": {labels[r]: r.replace("\\", "/") for r in roots},
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
             "resumed": s.get("resumed", False), "waiting": s.get("waiting", False),
             "wreason": s.get("wreason"), "assay": assay.get(s["session"])}
            for s in sessions
        ],
    }
    data_str = json.dumps(payload, indent=None)
    OUT.write_text(data_str, encoding="utf-8")
    print(f"Roots: {', '.join(labels[r] for r in roots)}")
    print(f"Sessions kept: {len(sessions)}  skipped(no cwd/empty): {skipped}")
    print(f"Scan cache: {reused} reused, {parsed} parsed")
    print(f"Date range: {payload['min_date']} -> {payload['max_date']}")
    print(f"Descriptions: {have}/{len(sessions)} present "
          f"({len(sessions) - have} need summarize.py / subagent pass)")
    print(f"Kindling (limit-unresumed + waiting-for-input): {len(kindling)} "
          f"[{sum(1 for s in kindling if assay.get(s['session']))} assayed]")
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

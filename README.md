# Development-Heatmap

A self-contained HTML viewer that maps your **Claude Code** session activity across a
folder as a heat-graded directory tree, with per-directory session lists and
LLM-written summaries of what happened in each session.

![concept](https://img.shields.io/badge/output-single%20double--clickable%20HTML-fd8d3c)

## What it does

- Walks Claude Code session logs (`~/.claude/projects/*/*.jsonl`) and builds a
  directory tree of everywhere you've worked under a chosen root.
- **Heat-grades each directory tile** (deep blue → orange, log-scaled) by either
  **token spend** or **user-prompt count** — toggle animates the recolor in place.
- **Date-range + 36h presets** prune the tree to only directories active in the window.
- **Click any directory** → a session list with start time, duration, token/prompt
  counts, and a 3-line LLM description of what each session accomplished.

The result is one **self-contained, double-clickable `session_heatmap.html`** — data
is embedded, so it works straight off `file://` with no server.

## Pipeline

```
scan_sessions.py  ──► digests.json   (deterministic: each session's gist)
                  └─► data.json + session_heatmap.html  (merges summaries.json)

summarize.py      ──► summaries.json  (LLM: 3-line description, CACHED by session id)
```

## Quick start

```bash
python scan_sessions.py          # first run auto-detects your working dirs, builds the viewer
python summarize.py              # (optional) add 3-line descriptions — needs an API key
python assay_kindling.py         # (optional) model judges if each kindling item is still useful
python scan_sessions.py          # re-run to merge descriptions/assay in
# open session_heatmap.html
```

Manage which directories are shown:

```bash
python scan_sessions.py --list-roots
python scan_sessions.py --add-root "C:/path/to/dir"
python scan_sessions.py --remove-root "C:/path/to/dir"
```

`scan_sessions.py` is **pure Python 3 stdlib** — no install needed. Session
descriptions need an LLM; see [`AGENTS.md`](./AGENTS.md) for the API-key path and the
subagent fallback (no key required when run inside an agent).

## Privacy

The generated artifacts (`data.json`, `digests.json`, `summaries.json`, and the built
`session_heatmap.html`) contain summaries, first-prompts, and **absolute local paths**
of every session — they are **git-ignored on purpose**. This repo ships the *tooling*;
you run it against your own machine. Don't commit the data outputs to a public repo.

## Portability

Paths derive from `Path.home()` at runtime — no hardcoded drive letters. The reader is
currently Claude-Code-specific (`~/.claude/projects` JSONL schema); porting to another
agent harness means swapping the reader in `scan_file()`. See `AGENTS.md`.

## License

See [`LICENSE`](./LICENSE).

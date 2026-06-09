# AGENTS.md — Development Heatmap

A self-contained HTML viewer that maps your Claude Code session activity across the
`~/Documents/AI` folder as a heat-graded directory tree, with a per-directory
session list. This file documents the **harness dependencies** an agent needs that
are **not present in an out-of-the-box codex / claude-code install**, plus the build
workflow.

## What's here

| File | Role | Deps |
|------|------|------|
| `scan_sessions.py` | Walks session logs → `data.json`, `digests.json`, and the HTML | **Python 3 stdlib only** |
| `summarize.py` | LLM 3-line session descriptions → `summaries.json` (cached) | **`anthropic` + API key** ⚠️ |
| `template.html` | The viewer app (data injected at build) | — |
| `session_heatmap.html` | **The deliverable** — self-contained, double-clickable | a browser |
| `data.json` / `digests.json` / `summaries.json` | build artifacts / cache | — |

## Pipeline

```
scan_sessions.py  ──► digests.json   (deterministic: each session's gist)
                  └─► data.json + session_heatmap.html  (merges summaries.json if present)

summarize.py      ──► summaries.json (LLM: 3-line description, CACHED by session id)
                  ↑ requires an LLM — see "Harness dependencies" below

re-run scan_sessions.py to fold new summaries into the viewer.
```

`summaries.json` is a cache keyed by session id. Both `scan_sessions.py` and
`summarize.py` only ever *add* missing entries, so re-runs after new sessions accrue
are cheap — you summarize the new sessions, not all of them.

## Harness dependencies (NOT out-of-the-box)

### 1. The session-log data source (claude-code-specific)
The scanner reads Claude Code transcript files at `~/.claude/projects/*/*.jsonl` and
relies on that schema: per-line `type` (`user`/`assistant`/`system`), `cwd`,
`timestamp`, and `message.usage` token fields. **A bare Codex install does not write
these files.** To port to another harness you must replace `scan_file()` /
`PROJECTS_DIR` with a reader for that harness's transcript format. Everything
downstream (tree, heat, viewer) is format-agnostic once `data.json` exists.

### 2. An LLM for session descriptions (`summarize.py`)
The 3-line "what happened" descriptions cannot be computed deterministically — they
need a model. Neither the `anthropic` SDK nor an API key ships by default. Two
supported ways to fill `summaries.json`:

**a) API path (portable, unattended):**
```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...      # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
python summarize.py                       # fills only missing descriptions (uses claude-haiku-4-5)
python scan_sessions.py                   # merge into viewer
```

**b) Subagent path (no API key — uses the agent harness itself):**
When running inside an agent that can spawn subagents (claude-code's Agent tool,
codex equivalents), fan out claudlings to summarize slices of `digests.json`:
1. `python scan_sessions.py` to (re)generate `digests.json`; note the key count.
2. Split the keys (in file order) into N batches. Dispatch N parallel subagents,
   each told to read `digests.json`, summarize its `keys[start:end]` slice into a
   **3-line-max, no-markdown** description, and Write `{session_id: desc}` JSON to
   `parts/part_<n>.json`.
3. Merge all `parts/part_*.json` into `summaries.json` (last-writer-wins per id;
   clamp each value to 3 lines), then re-run `scan_sessions.py`.

This subagent capability is the actual "harness dependency" when no key is present —
it substitutes the agent runtime for the API.

### 3. (Optional) Node.js
Only used for ad-hoc validation of the embedded JSON during development. **Not
required** to build or view. The scanner and viewer need only Python 3 + a browser.

## Build / regenerate

```bash
python scan_sessions.py          # always safe; rebuilds data + html from current logs
# then, if there are un-summarized sessions:
python summarize.py              # (API path)  OR  run the subagent path above
python scan_sessions.py          # merge new descriptions, rebuild html
```
Open `session_heatmap.html` in any browser. No server needed — data is embedded, so
local `file://` works (we inline rather than `fetch()` to dodge `file://` CORS).

## Conventions / gotchas
- **Paths are derived at runtime** (`Path.home()`), not hardcoded — portable across
  machines/OSes. The viewer is Windows-authored but pure HTML/JS.
- The viewer's heat color maps to **one** metric at a time (token spend *or* user
  prompts), toggled in the UI; the bottom-right key always reflects that one metric.
- Descriptions degrade gracefully: a session with no `desc` falls back to its cleaned
  first user prompt; the raw prompt is always available on hover in the detail view.

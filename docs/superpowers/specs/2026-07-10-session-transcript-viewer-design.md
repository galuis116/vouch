# Session Transcript Viewer — Design

Date: 2026-07-10
Status: Implemented (see `docs/superpowers/plans/2026-07-10-session-transcript-viewer.md`)
Repo: `vouch` (backend `src/vouch`, frontend `webapp`)

## Problem

The vouch console (`webapp`) can list captured agent sessions
(`kb.list_sessions`) but cannot show what actually happened inside one. A row
gives a title, a stage, and an observation count — nothing more. When an agent
files claims a reviewer often needs to see the reasoning: the prompts, the
assistant's replies, the tools it ran, and the diffs it made.

The `agentsview` project already renders exactly this for Claude Code / Codex
sessions. This feature ports **agentsview's rendering experience** into the
vouch console so a reviewer can open a session and read its full transcript.

### What this is NOT

- Not live-run rendering. The chat's "Claude mode" (`ChatView`) stays as-is;
  this feature is a read-only viewer for **already-captured** sessions.
- Not a re-implementation of agentsview's storage architecture. See
  "Relationship to agentsview" below — we copy the rendering, not the
  sync-into-a-database pipeline.

## Relationship to agentsview (what we copy, what we don't)

agentsview works in two stages:

1. **Ingest → SQLite.** A sync engine + file watcher parse the raw agent JSONL
   into normalized rows in a SQLite DB (`messages`, `tool_calls`,
   `tool_result_events`, FTS5). The raw file is parsed once at sync time.
2. **Serve → render.** A paginated REST API reads from the DB; the Svelte
   frontend segments `content` + `tool_calls` into typed blocks
   (`thinking` / `tool` / `code` / `skill` / `text`) client-side and renders
   them with per-block components.

We faithfully copy **stage 2's rendering vocabulary** (block types, per-tool
rendering, diffs, collapsibles, lazy subagents). We deliberately do **not**
copy stage 1: instead of syncing raw files into a database, the vouch backend
**parses the raw file on demand** when a session is opened. This yields the
same on-screen result with none of the sync/DB/watcher machinery, which is the
right trade for a viewer.

Consequences accepted for v1:

- Re-parses on each open (fine — a viewer opens one session at a time; large
  sessions are handled by capping + lazy subagent loading, below).
- No cross-session full-text search inside transcripts.
- If the raw file has been deleted, the viewer degrades to vouch's compact
  observations (below) rather than failing.

## Scope

In scope for v1:

- Agents: **Claude Code** (`~/.claude/projects/<escaped-cwd>/<id>.jsonl`) and
  **Codex** (rollouts under `$CODEX_HOME/sessions/...`), on the **same machine**
  as the vouch server.
- A new read-only RPC `kb.session_transcript`.
- A new frontend **Sessions** tab (master–detail) that lists sessions and
  renders a selected session's transcript at full fidelity.

Out of scope for v1:

- Remote / multi-machine sessions whose raw files are not on the server host
  (would require a transcript-upload pipeline).
- Persisting or indexing transcripts; cross-session search.
- Editing, pinning, exporting, or analytics over transcripts.

## Backend

### New RPC: `kb.session_transcript`

Params:

- `session_id` (string, required) — the captured session id.
- `agent` (string, optional) — `"claude"` | `"codex"`. When omitted, the
  locator auto-detects by searching both sources.

Success result (raw file found and parsed):

```jsonc
{
  "available": true,
  "source": { "agent": "claude", "path": "/home/u/.claude/projects/.../<id>.jsonl" },
  "session": {
    "id": "…",
    "cwd": "…",
    "git_branch": "…",
    "started_at": "ISO-8601",
    "ended_at": "ISO-8601",
    "model": "…",
    "tokens": { "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0 }
  },
  "messages": [
    {
      "role": "user" | "assistant",
      "id": "…",                 // message id (assistant), else null
      "model": "…",              // optional, assistant only
      "timestamp": "ISO-8601",   // optional
      "tokens": { "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0 },
      "blocks": [
        { "type": "text",     "text": "…" },
        { "type": "thinking", "text": "…" },
        // tool results are paired into their tool_use block server-side, so
        // the frontend renders input + output together (agentsview's ToolBlock).
        { "type": "tool_use", "id": "…", "name": "Bash", "input": { },
          "result": { "content": "…", "is_error": false,
                      "subagent_session_id": null } }   // null until paired
      ]
    }
  ],
  "truncated": false            // true if message cap hit (see limits)
}
```

Implementation note: the draft's separate `tool_result` block was dropped in
favor of pairing each `tool_result` into its originating `tool_use` block by id
during the single parse pass. The tool-result-only `user` entries are consumed,
not emitted as standalone messages. This matches agentsview's unified tool
block (input + output shown together) and keeps the frontend renderer simple.

Degraded result (raw file unavailable — deleted, off-machine, unreadable):

```jsonc
{
  "available": false,
  "reason": "raw transcript not found for session <id>",
  "observations": [ { "ts": "…", "tool": "Edit", "summary": "Edited types.go" } ]
}
```

`observations` are read from vouch's existing capture buffer
(`capture._read_observations`) when one still exists for the session; otherwise
an empty list. This is the honest fallback: it is agentsview's *rendering*
degraded to vouch's *data*.

Errors (structured JSONL envelope, matching every other handler):

- missing `session_id` → `missing_param` (a bare `p["session_id"]` KeyError,
  mapped by the dispatcher).
- `agent` present but not `claude`/`codex` → `invalid_request` (a raised
  `ValueError`).

Registered on all three surfaces to satisfy vouch's parity invariant
(`test_capabilities`): `jsonl_server.HANDLERS`, `capabilities.METHODS`, and the
`kb_session_transcript` MCP tool in `server.py`. `http_server` reuses
`jsonl_server.handle_request`, so it is covered by the JSONL registration.

Locator env overrides (used by tests, honored in prod): `VOUCH_CLAUDE_PROJECTS_DIR`
re-roots the Claude search; `CODEX_HOME` re-roots the Codex rollout search.

### New module: `src/vouch/transcript.py` (pure, fixture-tested)

Responsibilities, each a small pure function:

1. **Locate** the raw file for `(session_id, agent?)`:
   - Claude Code: glob `~/.claude/projects/*/<session_id>.jsonl` (the file stem
     is the session id). Subagent files live at
     `~/.claude/projects/*/<session_id>/subagents/**/*.jsonl`.
   - Codex: reuse `codex_rollout.find_rollout_by_session_id`.
   - Auto-detect tries Claude then Codex.
   - Honor `VOUCH_CLAUDE_PROJECTS_DIR` / `CODEX_HOME` overrides for tests.
   - `session_id` is validated against a UUID-shaped pattern before any glob so
     a hostile id cannot widen the search or traverse the tree.
2. **Parse + normalize** raw lines into the `messages[]` schema above.
   - Claude Code line schema (per JSONL entry): `message.role`,
     `message.model`, `message.usage.{input_tokens,output_tokens,
     cache_read_input_tokens,cache_creation_input_tokens}`, and
     `message.content[]` whose parts have `type` ∈
     `{text, thinking, tool_use, tool_result}`. `tool_use` carries
     `{id, name, input}`; `tool_result` (in user entries) carries
     `{tool_use_id, content, is_error}`. Session-level `cwd`, `gitBranch`,
     `timestamp` come from the entries. Subagent linkage via
     `toolUseResult.agentId` maps a `tool_result` to its child session id
     (mirrors agentsview's `subagentMap`).
   - Codex: `codex_rollout.parse_rollout` is lossy (compact observations only),
     so a dedicated `parse_codex_transcript` reads the raw `response_item`
     records — the canonical conversation stream: `message` (role user →
     `input_text`, assistant → `output_text`; developer/system boilerplate
     skipped), `function_call` / `custom_tool_call` + their `*_output` pairs,
     and `reasoning` (encrypted, so dropped). `session_meta` supplies
     `id`/`cwd`/`git.branch`/`timestamp`. `event_msg` records are UI mirrors and
     ignored to avoid duplication.
   - Malformed lines are skipped, not fatal (matches the existing stream
     parser's tolerance).
3. **Limits** (protect the server + browser):
   - Max file size read (config const, e.g. 25 MB); over → degraded result with
     reason.
   - Max messages returned (config const, e.g. 2000); over → `truncated: true`.
   - Per-block content is passed through; very large tool outputs are the
     browser's problem to collapse, not the server's to trim (agentsview keeps
     full content; we match).

Subagents are fetched lazily by a **second** `kb.session_transcript` call with
the child `session_id` (the frontend passes `subagent_session_id`). The backend
locator finds `~/.claude/projects/*/<parent>/subagents/**/<child>.jsonl` as well
as top-level files, so the same RPC serves both.

### Wiring

- Handler `_h_session_transcript(p)` registered in
  `src/vouch/jsonl_server.py` `HANDLERS` (and the HTTP surface in
  `http_server.py` if it maintains its own map).
- Add `"kb.session_transcript"` to `src/vouch/capabilities.py` method list
  (the `test_capabilities` drift test enforces this).
- Read-only: never calls `approve`/`propose`; unaffected by the review gate.

## Frontend (`webapp`, React + Tailwind v4)

Port agentsview's block vocabulary into React, styled with vouch's existing
semantic tokens (`paper` / `ink` / `accent` / `sepia` / `rule` / `ok`), reusing
the existing `Markdown` component for text blocks and `lucide-react` icons.

### Route + entry point

- New route `/sessions` and `/sessions/:id` in `App.tsx`; a **Sessions** entry
  in the nav (`Shell`), gated on `hasMethod('kb.session_transcript')` like the
  other capability-gated tabs.
- `SessionsView` — master–detail. Left: list from `kb.list_sessions`
  (`useFanout`), newest first; rows with a non-null `session_id` are openable,
  null ones are shown disabled. Right: `TranscriptView` for the selected id.

### Components (each maps to an agentsview equivalent)

| vouch (new) | agentsview reference | behavior |
| --- | --- | --- |
| `TranscriptView` | `MessageList` | fetches `kb.session_transcript(id)`, renders `messages[]`; shows session vitals header (model, tokens, cwd, branch) |
| `MessageBlock` | `MessageContent` | role chrome, model badge, tokens, timestamp; dispatches blocks |
| `ThinkingBlock` | `ThinkingBlock` | collapsible "Thinking" |
| `ToolBlock` | `ToolBlock` | collapsible; per-tool rendering; error styling |
| `DiffView` | ToolBlock diff-view | +/− line rendering for Edit/Write |
| `CodeBlock` | `CodeBlock` | fenced code |
| `TextBlock` | markdown path | reuse existing `Markdown` |

Per-tool rendering inside `ToolBlock` (parity with agentsview):

- `Bash` / `run_command` → command line + collapsible stdout.
- `Edit` / `Update` / `MultiEdit` → `DiffView`.
- `Write` → created-file content.
- `Read` / `Grep` / `Glob` → compact summary (path/pattern) + collapsible body.
- `Task` / `Agent` → labeled subagent step; when the paired result carries a
  `subagent_session_id`, a **"view subagent"** button pushes the child onto an
  in-`TranscriptView` back-stack and re-fetches `kb.session_transcript(child)`.
- Unknown tools (and every tool's raw input) → collapsible pretty-printed JSON.

Shipped simplification: `Read`/`Grep`/`Glob` render a one-line headline plus the
collapsible output; a dedicated `TodoWrite` checklist renderer was deferred
(TodoWrite falls through to the JSON input view). Easy follow-up if wanted.

### Degraded rendering

When `available === false`, `TranscriptView` shows a notice ("original
transcript unavailable — showing captured activity") and renders the
`observations` as a compact tool timeline.

### Client library

- `webapp/src/lib/transcript.ts` — types for the normalized schema + a thin
  `fetchTranscript(conn, id)` wrapper over `rpc('kb.session_transcript', …)`.
  No new transport; reuses `/proxy/rpc`.

## Error handling summary

- Unknown / null session id → structured RPC error surfaced as a `Toast` + an
  `ErrorCard` in the detail pane.
- File missing/oversized/unreadable → `available:false` degraded result.
- Malformed transcript lines → skipped in the parser.
- Endpoint doesn't advertise the method → nav hidden / disabled (capability
  gate), never a hard failure.

## Testing

Backend (vouch conventions: `pytest`, `mypy src`, `ruff check`):

- `tests/test_session_transcript.py` — table/fixture tests over the parser with
  small committed **Claude Code** and **Codex** JSONL fixtures: text, thinking,
  tool_use→tool_result pairing, is_error, subagent linkage, model/tokens, cwd/
  branch extraction; malformed-line tolerance; size/message caps → `truncated`.
- Locator tests using `VOUCH_CLAUDE_PROJECTS_DIR` / `CODEX_HOME` pointed at
  `tmp_path` fixtures; auto-detect order; subagent-file resolution.
- Degradation test: no raw file, buffer present → `available:false` +
  observations; no raw file, no buffer → empty observations.
- RPC envelope test `tests/test_session_transcript.py` asserting the JSONL
  envelope shape; capabilities drift covered by `test_capabilities`.

Frontend (`vitest` + Testing Library; one Playwright smoke):

- Component tests per block (`ToolBlock` per-tool branches incl. `DiffView`,
  `ThinkingBlock` collapse, `Task` subagent lazy-load with a mocked rpc),
  `TranscriptView` happy path + degraded path, `SessionsView` list + disabled
  null rows.
- `webapp/e2e/` smoke: open Sessions, pick a row, see the rendered transcript.
  It stubs `/proxy/*` via Playwright `page.route` (health, capabilities,
  `kb.list_pending`, `kb.list_sessions`, `kb.session_transcript`) so it drives
  the real frontend independent of the backend build — the local `vouch` on
  PATH is an editable install of a different checkout without the new RPC.
- Tests assert rendered behavior, not implementation strings
  (`testing-without-tautologies`).

## Conventions / guardrails (this repo)

- Follow `vouch` `AGENTS.md`, not agentsview's `CLAUDE.md`: conventional
  commits `<type>(<scope>): …`; run `pytest --ignore=tests/embeddings`,
  `mypy src`, `ruff check src tests` before shipping. **No
  `Co-Authored-By: <AI tool>` trailer.** No secrets/paths-as-PII in commits.
- Work on a feature branch (ask before creating it); do not commit to `main`
  without permission; do not merge.

## Phasing

1. Backend: `transcript.py` (Claude locator + parser) + RPC + capabilities +
   tests.
2. Frontend: `SessionsView` + `TranscriptView` + block components + client lib
   + tests, wired to phase-1 RPC.
3. Codex source (reuse `codex_rollout`) + subagent lazy expansion + degraded
   fallback + e2e smoke.

## Resolved (as shipped)

- Entry point: a dedicated **Sessions** tab in the console nav
  (`ScrollText` icon), gated per-row on `hasMethod('kb.session_transcript')`.
- Layout: master–detail — session list on the left, transcript on the right.
- Codex shipped in v1 (phase 3) alongside Claude Code, via a dedicated
  `response_item` parser (not the lossy `parse_rollout`).

## Deferred (possible follow-ups)

- Dedicated `TodoWrite` checklist renderer (currently the JSON fallback).
- Rendering `reasoning`/`thinking` when a session persists plaintext (VSCode/SDK
  sessions store only the encrypted signature, so thinking blocks are dropped).
- Remote / multi-machine transcript access (still out of scope).

"""Locate and parse raw agent session transcripts on demand.

Given a captured session id, find the raw JSONL the agent wrote (Claude Code
under ``~/.claude/projects``, Codex rollouts under ``$CODEX_HOME/sessions``)
and normalize it into a block schema the vouch console renders. Read-only:
never writes to the KB. When the raw file is gone we degrade to vouch's
compact capture observations instead.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Session ids are UUID-shaped; reject anything else so a hostile id can't
# widen a glob or traverse out of the projects tree.
_VALID_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _claude_projects_root() -> Path:
    env = os.environ.get("VOUCH_CLAUDE_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def find_claude_file(session_id: str) -> Path | None:
    """The raw Claude Code JSONL for ``session_id``, or None.

    Claude names each session file ``<id>.jsonl`` under a per-cwd project
    dir; subagent transcripts live under ``<parent>/subagents/**``. The file
    stem is the id, so a literal name match (no id interpolation into a glob)
    locates it.
    """
    if not _VALID_ID.match(session_id):
        return None
    root = _claude_projects_root()
    if not root.is_dir():
        return None
    name = f"{session_id}.jsonl"
    for project in root.iterdir():
        if not project.is_dir():
            continue
        top = project / name
        if top.is_file():
            return top
    for candidate in root.glob(f"*/*/subagents/**/{name}"):
        if candidate.is_file():
            return candidate
    return None


def _norm_tokens(usage: dict[str, Any]) -> dict[str, int]:
    def i(key: str) -> int:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    return {
        "input": i("input_tokens"),
        "output": i("output_tokens"),
        "cache_read": i("cache_read_input_tokens"),
        "cache_creation": i("cache_creation_input_tokens"),
    }


def _result_text(content: Any) -> str:
    """tool_result.content is a string, or a list of {type:text,text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def parse_claude_transcript(path: Path, *, max_messages: int = 2000) -> dict[str, Any]:
    """Parse a Claude Code JSONL into the normalized transcript schema.

    Single forward pass: assistant content blocks that share a
    ``message.id`` merge into one logical message; a later ``tool_result``
    (in a user entry) is paired into the matching ``tool_use`` block by id and
    its user entry is not emitted as a standalone message.
    """
    messages: list[dict[str, Any]] = []
    tool_by_id: dict[str, dict[str, Any]] = {}
    session: dict[str, Any] = {
        "id": path.stem, "agent": "claude", "cwd": None, "git_branch": None,
        "title": None, "started_at": None, "ended_at": None, "model": None,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    truncated = False
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current["blocks"]:
            messages.append(current)
        current = None

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if session["cwd"] is None and isinstance(obj.get("cwd"), str):
                session["cwd"] = obj["cwd"]
            if session["git_branch"] is None and isinstance(obj.get("gitBranch"), str):
                session["git_branch"] = obj["gitBranch"]
            ts = obj.get("timestamp")
            if isinstance(ts, str):
                if session["started_at"] is None:
                    session["started_at"] = ts
                session["ended_at"] = ts
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                session["title"] = obj["aiTitle"]
                continue
            if t not in ("user", "assistant"):
                continue
            if len(messages) >= max_messages:
                truncated = True
                break
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")

            if t == "assistant":
                mid = msg.get("id") if isinstance(msg.get("id"), str) else None
                if current is None or current.get("id") != mid:
                    flush()
                    raw_usage = msg.get("usage")
                    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
                    model = msg.get("model") if isinstance(msg.get("model"), str) else None
                    if model and session["model"] is None:
                        session["model"] = model
                    current = {
                        "role": "assistant", "id": mid, "model": model,
                        "timestamp": ts if isinstance(ts, str) else None,
                        "tokens": _norm_tokens(usage), "blocks": [],
                    }
                    tok = current["tokens"]
                    for k in session["tokens"]:
                        session["tokens"][k] += tok[k]
                parts = content if isinstance(content, list) else []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "thinking":
                        text = str(part.get("thinking", "")).strip()
                        if text:
                            current["blocks"].append({"type": "thinking", "text": text})
                    elif ptype == "text":
                        text = str(part.get("text", "")).strip()
                        if text:
                            current["blocks"].append({"type": "text", "text": text})
                    elif ptype == "tool_use":
                        tid = part.get("id")
                        raw_input = part.get("input")
                        block: dict[str, Any] = {
                            "type": "tool_use", "id": tid,
                            "name": str(part.get("name", "")),
                            "input": raw_input if isinstance(raw_input, dict) else {},
                            "result": None,
                        }
                        current["blocks"].append(block)
                        if isinstance(tid, str):
                            tool_by_id[tid] = block
                continue

            # user entry
            flush()
            if isinstance(content, str):
                text = content.strip()
                if text:
                    messages.append({
                        "role": "user", "id": None, "model": None,
                        "timestamp": ts if isinstance(ts, str) else None,
                        "tokens": None, "blocks": [{"type": "text", "text": text}],
                    })
                continue
            parts = content if isinstance(content, list) else []
            user_blocks: list[dict[str, Any]] = []
            agent_id = None
            tur = obj.get("toolUseResult")
            if isinstance(tur, dict) and isinstance(tur.get("agentId"), str):
                agent_id = tur["agentId"]
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result":
                    tid = part.get("tool_use_id")
                    paired = tool_by_id.get(tid) if isinstance(tid, str) else None
                    if paired is not None:
                        paired["result"] = {
                            "content": _result_text(part.get("content")),
                            "is_error": bool(part.get("is_error", False)),
                            "subagent_session_id": agent_id,
                        }
                elif part.get("type") == "text":
                    text = str(part.get("text", "")).strip()
                    if text:
                        user_blocks.append({"type": "text", "text": text})
            if user_blocks:
                messages.append({
                    "role": "user", "id": None, "model": None,
                    "timestamp": ts if isinstance(ts, str) else None,
                    "tokens": None, "blocks": user_blocks,
                })
    flush()
    return {"session": session, "messages": messages, "truncated": truncated}

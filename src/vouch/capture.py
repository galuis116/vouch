"""Auto-capture Claude Code sessions into review-gated summaries.

Passive harvest -> mechanical rollup -> one PENDING page proposal. No LLM.
`observe` appends compact observations to an ephemeral, gitignored scratch
buffer (`.vouch/captures/<session>.jsonl`); `finalize` rolls the buffer plus a
git-diff backstop into a single session-summary page proposal that a human
approves like any other write. Never calls approve() — the review gate stays
intact. See docs/superpowers/specs/2026-07-01-vouch-session-autocapture-design.md
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .storage import KBStore

DEFAULT_ENABLED = True
DEFAULT_MIN_OBSERVATIONS = 3
DEFAULT_DEDUP_WINDOW_SECONDS = 60.0
CAPTURE_ACTOR = "vouch-capture"
CAPTURE_PAGE_TYPE = "session"


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = DEFAULT_ENABLED
    min_observations: int = DEFAULT_MIN_OBSERVATIONS
    dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS


def load_config(store: KBStore) -> CaptureConfig:
    """Read ``capture:`` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text())
    except (OSError, yaml.YAMLError):
        return CaptureConfig()
    if not isinstance(loaded, dict):
        return CaptureConfig()
    raw = loaded.get("capture")
    if not isinstance(raw, dict):
        return CaptureConfig()
    return CaptureConfig(
        enabled=bool(raw.get("enabled", DEFAULT_ENABLED)),
        min_observations=int(raw.get("min_observations", DEFAULT_MIN_OBSERVATIONS)),
        dedup_window_seconds=float(
            raw.get("dedup_window_seconds", DEFAULT_DEDUP_WINDOW_SECONDS)
        ),
    )


def captures_dir(store: KBStore) -> Path:
    return store.kb_dir / "captures"


def buffer_path(store: KBStore, session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_").strip() or "unknown"
    return captures_dir(store) / f"{safe}.jsonl"


_OBSERVED_TOOLS = frozenset({
    "Read", "Edit", "Write", "Update", "Bash",
    "Grep", "Glob", "WebFetch", "WebSearch", "Task", "NotebookEdit",
})


def _read_observations(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _dedup_key(tool: str, summary: str) -> str:
    return f"{tool}\x00{summary}"


def observe(
    store: KBStore,
    session_id: str,
    *,
    tool: str,
    summary: str,
    files: list[str] | None = None,
    cmd: str | None = None,
    now: float | None = None,
    config: CaptureConfig | None = None,
) -> bool:
    """Append one observation to the session buffer. Returns True if written."""
    cfg = config or load_config(store)
    if not cfg.enabled:
        return False
    ts = time.time() if now is None else now
    path = buffer_path(store, session_id)
    key = _dedup_key(tool, summary)
    for obs in reversed(_read_observations(path)):
        if ts - float(obs.get("ts", 0.0)) > cfg.dedup_window_seconds:
            break
        if _dedup_key(str(obs.get("tool", "")), str(obs.get("summary", ""))) == key:
            return False
    record: dict[str, Any] = {"ts": ts, "tool": tool, "summary": summary}
    if files:
        record["files"] = files
    if cmd:
        record["cmd"] = cmd
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return True


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] or path


def summarize_tool(
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    tool_response: object,
) -> dict[str, Any] | None:
    """Turn a PostToolUse payload into a compact observation, or None to skip."""
    if not tool_name or tool_name not in _OBSERVED_TOOLS:
        return None
    ti = tool_input or {}
    out: dict[str, Any] = {"tool": tool_name}
    fp = ti.get("file_path")
    if isinstance(fp, str) and fp:
        out["files"] = [fp]
    if tool_name in {"Read", "Edit", "Write", "Update", "NotebookEdit"}:
        name = _basename(fp) if isinstance(fp, str) and fp else "file"
        verb = {"Read": "Read", "Write": "Created"}.get(tool_name, "Edited")
        out["summary"] = f"{verb} {name}"
    elif tool_name == "Bash":
        cmd = ti.get("command")
        short = str(cmd).splitlines()[0][:60] if cmd else "command"
        if cmd:
            out["cmd"] = str(cmd)[:200]
        text = str(tool_response).lower()
        failed = "error" in text or "failed" in text
        out["summary"] = f"Command failed: {short}" if failed else f"Ran: {short}"
    elif tool_name in {"Grep", "Glob"}:
        out["summary"] = f"{tool_name} {str(ti.get('pattern', ''))[:40]}"
    elif tool_name in {"WebFetch", "WebSearch"}:
        target = ti.get("url") or ti.get("query") or ""
        out["summary"] = f"Fetched: {str(target)[:60]}"
    else:  # Task
        out["summary"] = f"{tool_name} completed"
    return out

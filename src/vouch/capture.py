"""Auto-capture Claude Code sessions into review-gated summaries.

Passive harvest -> mechanical rollup -> one PENDING page proposal. No LLM.
`observe` appends compact observations to an ephemeral, gitignored scratch
buffer (`.vouch/captures/<session>.jsonl`); `finalize` rolls the buffer plus a
git-diff backstop into a single session-summary page proposal that a human
approves like any other write. Never calls approve() — the review gate stays
intact. See docs/superpowers/specs/2026-07-01-vouch-session-autocapture-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

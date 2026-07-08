"""Summarize a session's observation buffer into review-gated pages.

Host-blind: reads only the normalized observation buffer
(`.vouch/captures/<id>.jsonl`) that every host adapter writes via
`capture.observe`, never a host transcript. Small sessions get one mechanical
rollup page (reusing `capture.build_summary_body`); large sessions get an LLM
topical split into several `type: session` pages. Every page is a PENDING
proposal — `approve()` is never called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import yaml

from .storage import KBStore

logger = logging.getLogger(__name__)

SPLIT_ACTOR = "session-split"

DEFAULT_THRESHOLD_OBSERVATIONS = 40
DEFAULT_MAX_PAGES = 6
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_INPUT_CHARS = 60000


class SplitConfigError(Exception):
    """The split cannot run (no resolvable llm_cmd)."""


@dataclass(frozen=True)
class SplitConfig:
    enabled: bool = True
    llm_cmd: str | None = None
    threshold_observations: int = DEFAULT_THRESHOLD_OBSERVATIONS
    max_pages: int = DEFAULT_MAX_PAGES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS


def _coerce(value: Any, default: Any, cast: Any) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def load_split_config(store: KBStore) -> SplitConfig:
    """Read `capture.split` from config.yaml; fall back to defaults."""
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return SplitConfig()
    if not isinstance(loaded, dict):
        return SplitConfig()
    cap = loaded.get("capture")
    raw = cap.get("split") if isinstance(cap, dict) else None
    if not isinstance(raw, dict):
        return SplitConfig()
    llm_cmd = raw.get("llm_cmd")
    return SplitConfig(
        enabled=bool(raw.get("enabled", True)),
        llm_cmd=str(llm_cmd) if llm_cmd else None,
        threshold_observations=_coerce(
            raw.get("threshold_observations", DEFAULT_THRESHOLD_OBSERVATIONS),
            DEFAULT_THRESHOLD_OBSERVATIONS, int),
        max_pages=_coerce(raw.get("max_pages", DEFAULT_MAX_PAGES), DEFAULT_MAX_PAGES, int),
        timeout_seconds=_coerce(
            raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            DEFAULT_TIMEOUT_SECONDS, float),
        max_input_chars=_coerce(
            raw.get("max_input_chars", DEFAULT_MAX_INPUT_CHARS),
            DEFAULT_MAX_INPUT_CHARS, int),
    )

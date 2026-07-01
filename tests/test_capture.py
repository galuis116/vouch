"""Auto-capture: config, buffer, observe, finalize."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import capture as cap
from vouch.storage import KBStore, _starter_config


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_load_config_defaults(store: KBStore) -> None:
    cfg = cap.load_config(store)
    assert cfg.enabled is True
    assert cfg.min_observations == 3
    assert cfg.dedup_window_seconds == 60.0


def test_load_config_reads_override(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  enabled: false\n  min_observations: 5\n"
    )
    cfg = cap.load_config(store)
    assert cfg.enabled is False
    assert cfg.min_observations == 5


def test_buffer_path_under_captures_dir(store: KBStore) -> None:
    p = cap.buffer_path(store, "sess-123")
    assert p == store.kb_dir / "captures" / "sess-123.jsonl"


def test_starter_config_has_capture_namespace() -> None:
    assert _starter_config()["capture"]["enabled"] is True


def test_init_gitignores_captures(tmp_path: Path) -> None:
    kb = KBStore.init(tmp_path)
    assert "captures/" in (kb.kb_dir / ".gitignore").read_text()


def test_observe_appends_line(store: KBStore) -> None:
    wrote = cap.observe(store, "s1", tool="Edit", summary="Edited a.py", now=100.0)
    assert wrote is True
    lines = cap.buffer_path(store, "s1").read_text().splitlines()
    assert len(lines) == 1
    assert "Edited a.py" in lines[0]


def test_observe_dedups_within_window(store: KBStore) -> None:
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=100.0)
    # identical within 60s window -> skipped
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=130.0) is False
    # same key past the window -> written again
    assert cap.observe(store, "s1", tool="Read", summary="Read a.py", now=200.0)
    assert len(cap.buffer_path(store, "s1").read_text().splitlines()) == 2


def test_observe_noop_when_disabled(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  enabled: false\n")
    assert cap.observe(store, "s1", tool="Edit", summary="x") is False
    assert not cap.buffer_path(store, "s1").exists()


def test_summarize_tool_skips_unobserved() -> None:
    assert cap.summarize_tool("mcp__vouch__kb_search", {}, "") is None


def test_summarize_tool_edit() -> None:
    obs = cap.summarize_tool("Edit", {"file_path": "/repo/src/a.py"}, "ok")
    assert obs is not None
    assert obs["tool"] == "Edit"
    assert obs["files"] == ["/repo/src/a.py"]
    assert "a.py" in obs["summary"]


def test_summarize_tool_bash_flags_error() -> None:
    obs = cap.summarize_tool("Bash", {"command": "pytest"}, "1 failed, error")
    assert obs is not None
    assert obs["cmd"] == "pytest"
    assert "failed" in obs["summary"].lower()

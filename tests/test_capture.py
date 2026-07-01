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

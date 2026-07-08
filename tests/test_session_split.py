"""Host-blind session summarization: size gate, mechanical rollup, LLM split."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import session_split
from vouch.session_split import SplitConfig, load_split_config
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_split_config_defaults(store: KBStore) -> None:
    cfg = load_split_config(store)
    assert cfg == SplitConfig()
    assert cfg.threshold_observations == 40
    assert cfg.max_pages == 6
    assert cfg.enabled is True


def test_split_config_reads_override(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n    threshold_observations: 5\n    max_pages: 2\n"
        "    llm_cmd: \"cat /dev/null\"\n",
        encoding="utf-8",
    )
    cfg = load_split_config(store)
    assert cfg.threshold_observations == 5
    assert cfg.max_pages == 2
    assert cfg.llm_cmd == "cat /dev/null"


def test_split_config_malformed_yaml_falls_back(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  split:\n  - not-a-mapping\n", encoding="utf-8")
    assert load_split_config(store) == SplitConfig()


def test_split_config_typo_coerces_to_default(store: KBStore) -> None:
    store.config_path.write_text(
        "capture:\n  split:\n    max_pages: six\n", encoding="utf-8"
    )
    assert load_split_config(store).max_pages == 6

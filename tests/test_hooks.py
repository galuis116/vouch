"""The per-prompt hook injects relevant KB context with zero tool calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import context, health, hooks
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"e")
    s.put_claim(Claim(id="c1", text="deploys run on tuesdays via ci", evidence=[src.id]))
    health.rebuild_index(s)
    return s


def _force_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "deploys run on tuesdays via ci", 0.9)],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])


def test_empty_prompt_injects_nothing(store: KBStore) -> None:
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": ""})) == ""
    assert hooks.build_claude_prompt_hook(store, "") == ""


def test_relevant_prompt_yields_additional_context(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    env = json.loads(out)
    assert env["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "tuesdays" in env["hookSpecificOutput"]["additionalContext"]


def test_raw_non_json_stdin_is_tolerated(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, "when do deploys run")
    assert "tuesdays" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_no_hits_injects_nothing(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "zzznomatch"})) == ""

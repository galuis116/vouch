"""Tests for the dual-solve web runner (the SPA backend)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vouch.storage import KBStore
from vouch.web import create_app

pytest.importorskip("fastapi", reason="dual-solve web needs the [web] extra")

from fastapi.testclient import TestClient


@pytest.fixture
def git_kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    # dual-solve needs a git repo; the kb lives at its root.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _client(git_kb: KBStore, *, enabled: bool = True) -> TestClient:
    app = create_app(str(git_kb.root), allow_dual_solve=enabled)
    return TestClient(app)


def test_dual_solve_page_renders_when_enabled(git_kb):
    r = _client(git_kb).get("/dual-solve")
    assert r.status_code == 200
    assert "dual-solve-app" in r.text  # the Vue mount point


def test_dual_solve_routes_absent_when_disabled(git_kb):
    r = _client(git_kb, enabled=False).get("/dual-solve")
    assert r.status_code == 404

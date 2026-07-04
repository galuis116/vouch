"""Guards on the company-brain adapter prompts and intake modules.

The NL layer lives host-side as prompt files, so the strongest deterministic
guarantee available is textual: every brain command must carry the explicit
never-approve instruction, and every registered skill path must exist. The
intake modules get the structural version: no import path to approve().
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = REPO_ROOT / "adapters" / "claude-code" / ".claude" / "commands"

BRAIN_COMMANDS = [
    "vouch-ask.md",
    "vouch-remember.md",
    "vouch-record.md",
    "vouch-followup.md",
    "vouch-standup.md",
]


@pytest.mark.parametrize("name", BRAIN_COMMANDS)
def test_brain_command_pins_the_never_approve_rule(name: str) -> None:
    body = (COMMANDS_DIR / name).read_text(encoding="utf-8")
    assert "kb_approve" in body, f"{name} must state the approve rule explicitly"
    assert "Never call" in body, f"{name} lost its never-approve instruction"
    # proposing is the only write verb a brain prompt may teach
    assert "kb_propose" in body or "kb_digest" in body or "kb_context" in body


def test_manifest_skills_all_exist() -> None:
    manifest = json.loads((REPO_ROOT / "openclaw.plugin.json").read_text(encoding="utf-8"))
    for rel in manifest["skills"]:
        assert (REPO_ROOT / rel).is_file(), f"manifest skills entry missing: {rel}"


@pytest.mark.parametrize("module", ["fetch", "inbox", "notify"])
def test_intake_modules_have_no_approve_import(module: str) -> None:
    source = (REPO_ROOT / "src" / "vouch" / f"{module}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            assert "approve" not in names, f"{module}.py imports approve"
            assert node.module != "vouch.lifecycle", f"{module}.py imports lifecycle"
        if isinstance(node, ast.Attribute):
            assert node.attr != "approve", f"{module}.py references .approve"

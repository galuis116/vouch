"""Capabilities descriptor — must match the JSONL handler registration."""

from __future__ import annotations

from vouch import capabilities
from vouch.jsonl_server import HANDLERS


def test_capabilities_matches_jsonl_handlers() -> None:
    caps = capabilities.capabilities()
    declared = set(caps.methods)
    implemented = set(HANDLERS.keys())
    assert declared == implemented, (
        f"capabilities/handlers mismatch: "
        f"missing handlers={declared - implemented}, "
        f"missing capabilities={implemented - declared}"
    )


def test_mcp_tools_match_methods() -> None:
    """Every MCP kb_* tool maps to a capabilities method and vice-versa.

    Closes the MCP half of the 3-surface parity invariant that the JSONL
    check above did not cover. Uses the unfiltered server object (profiles
    apply only in run_stdio).
    """
    from vouch.server import mcp

    tool_names = {n for n in mcp._tool_manager._tools if n.startswith("kb_")}
    as_methods = {"kb." + n.split("_", 1)[1] for n in tool_names}
    declared = set(capabilities.METHODS)
    assert as_methods == declared, (
        f"mcp/methods mismatch: "
        f"missing tools={declared - as_methods}, "
        f"undeclared tools={as_methods - declared}"
    )

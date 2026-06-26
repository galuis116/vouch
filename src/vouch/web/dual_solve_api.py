"""dual-solve web runner: routes mounted into the review-ui under an explicit
opt-in flag. The blocking engine work runs in a threadpool; progress streams
over the review-ui's existing websocket. The review gate is preserved -- the
choose step only ever proposes to the kb.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .. import dual_solve as ds
from ..auto_pr import SubprocessRunner


def register(
    app: FastAPI,
    *,
    store: Any,
    hub: Any,
    auth: Any,
    guarded: list,
    render: Callable[[Request, str, dict[str, Any]], Any],
    reviewer: Callable[[], str],
    enabled: bool,
) -> None:
    """Mount the dual-solve routes. No-op unless ``enabled``."""
    if not enabled:
        return
    runner = SubprocessRunner()
    # fail fast at app-build time if we're not in a git repo: dual-solve can't
    # create worktrees otherwise.
    git_root = ds.repo_root(runner, store.root)
    app.state.dual_solve_git_root = str(git_root)

    @app.get("/dual-solve", response_class=HTMLResponse, dependencies=guarded)
    def dual_solve_page(request: Request) -> Any:
        return render(request, "dual_solve.html", {"active": "dual-solve"})

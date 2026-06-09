"""HTTP transport for `vouch serve` (VEP-0004 — final spec-compliant shape).

A single ASGI application that speaks three transport shapes against the same
kb.* surface:

* **`POST /mcp`** (and `/messages` alias) — MCP-over-Streamable-HTTP, the
  protocol Claude.ai Custom Connectors, Claude mobile (write), Anthropic
  Managed Agents, the Messages-API `mcp_servers` field, and Computer Use
  all speak. Implemented by mounting the FastMCP `StreamableHTTPASGIApp`
  built from `vouch.server.mcp`.
* **`POST /rpc`** — the lightweight vouch-native JSONL envelope shipped in
  #94 / #104. Kept identical to the prior contract so existing tooling keeps
  working.
* **`GET /healthz`** and **`GET /health`** — liveness probes (the latter is
  what Claude.ai's connector validator looks for).
* **`GET /capabilities`** — kb.capabilities JSON.

Safe by default: binds 127.0.0.1 and refuses any non-loopback bind unless a
bearer token (or multi-token accept-list) is configured AND --allow-public is
passed. Bearer auth gates `/rpc` and `/mcp` (and `/messages`); the health and
capabilities endpoints are intentionally unauthenticated so probes from the
outside still work without leaking credentials into a monitoring config.

Multi-token mode is the production path for fleets: every agent in the fleet
gets its own credential, the server validates by accept-list membership, and
operators rotate one token at a time without taking the service offline. The
accept-list is read from the CLI (`--token` for the legacy single value) or
from `config.yaml`:

    serve:
      bearer_tokens:
        - alpha
        - beta
      # OR — env-var reference keeps the secret out of git:
      bearer_token: env:VOUCH_TOKEN

TLS is intentionally not terminated in-process: stick a reverse proxy
(Cloudflare Tunnel, fly.io's frontend, nginx) in front for any public
deployment. See `adapters/http-tunnel/` for ready-to-deploy templates.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from . import jsonl_server
from .capabilities import capabilities as build_caps

log = logging.getLogger(__name__)

DEFAULT_PORT = 8731
MAX_BODY_BYTES = 4 * 1024 * 1024  # reject oversized bodies before reading
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Public paths bypass the bearer gate: liveness + capabilities advertisement
# need to be reachable by probes that can't reasonably be expected to carry a
# credential (Claude.ai's connector validator, fly.io's healthcheck, etc.).
_PUBLIC_PATHS = frozenset({"/healthz", "/health", "/capabilities"})


# --- config.yaml `serve:` section ----------------------------------------


class ServeConfigError(ValueError):
    """`serve:` section in config.yaml could not be resolved."""


@dataclass(frozen=True)
class ServeConfig:
    """Resolved `serve:` section from config.yaml.

    ``tokens`` is the flattened accept-list — `bearer_token` (singular) is
    expanded into the list and any `env:VAR` references are resolved.
    Order matters only for documentation; matching is set-membership.
    """
    tokens: list[str] = field(default_factory=list)


def _resolve_token(raw: str) -> str:
    """Turn ``env:VAR`` into ``os.environ["VAR"]``; pass plain strings through.

    Keeping literal tokens out of committed config files is the whole point of
    the ``env:`` form — a config that references a missing env var fails loudly
    rather than silently substituting empty string.
    """
    if isinstance(raw, str) and raw.startswith("env:"):
        var = raw.removeprefix("env:").strip()
        if not var:
            raise ServeConfigError("bearer_token value 'env:' must be followed by a variable name")
        val = os.environ.get(var)
        if val is None:
            raise ServeConfigError(
                f"bearer_token references env:{var} but the variable is unset"
            )
        return val
    return raw


def load_serve_config(path: Path) -> ServeConfig:
    """Read ``serve:`` from a config.yaml file. Missing file or missing
    section returns an empty ``ServeConfig`` — both are valid states."""
    if not path.exists():
        return ServeConfig()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ServeConfigError(f"could not parse {path}: {e}") from e
    if not isinstance(raw, dict):
        return ServeConfig()
    serve = raw.get("serve")
    if not isinstance(serve, dict):
        return ServeConfig()

    tokens: list[str] = []
    if "bearer_tokens" in serve:
        bt = serve["bearer_tokens"]
        if not isinstance(bt, list):
            raise ServeConfigError("serve.bearer_tokens must be a list of strings")
        for t in bt:
            if not isinstance(t, str) or not t.strip():
                raise ServeConfigError("serve.bearer_tokens entries must be non-empty strings")
            tokens.append(_resolve_token(t))
    if "bearer_token" in serve:
        bt = serve["bearer_token"]
        if not isinstance(bt, str) or not bt.strip():
            raise ServeConfigError("serve.bearer_token must be a non-empty string")
        tokens.append(_resolve_token(bt))

    # Drop any empties that survived (resolve_token never returns empty, but
    # defence-in-depth -- a bad token would otherwise turn into a silent
    # "everyone passes the gate" case if it ever reached the matcher).
    tokens = [t for t in tokens if t]
    return ServeConfig(tokens=tokens)


# --- request handlers (legacy /rpc + health + capabilities) ---------------


def _json(code: int, obj: Any) -> JSONResponse:
    return JSONResponse(obj, status_code=code)


def _error_payload(err_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": err_code, "message": message}}


async def _healthz(_request: Request) -> JSONResponse:
    return _json(200, {"ok": True})


async def _capabilities(_request: Request) -> JSONResponse:
    return _json(200, build_caps().model_dump(mode="json"))


async def _rpc(request: Request) -> JSONResponse:
    """Legacy vouch-native /rpc envelope from PR #104.

    Kept verbatim in semantics so vouch-aware clients pinned to it don't break
    when this server moves to ASGI. The MCP-spec /mcp endpoint is the new
    primary surface for AI agents.
    """
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return _json(400, _error_payload("invalid_request", "bad Content-Length"))
    if length < 0:
        return _json(400, _error_payload(
            "invalid_request", "Content-Length cannot be negative",
        ))
    if length > MAX_BODY_BYTES:
        return _json(413, _error_payload("invalid_request", "request body too large"))

    raw = await request.body()
    try:
        envelope = json.loads(raw or b"{}")
    except json.JSONDecodeError as e:
        return _json(400, _error_payload("invalid_request", f"invalid JSON: {e}"))
    if not isinstance(envelope, dict):
        return _json(400, _error_payload(
            "invalid_request", "envelope must be a JSON object",
        ))

    agent = request.headers.get("X-Vouch-Agent")
    reset = jsonl_server._actor.set(agent) if agent else None
    try:
        response = jsonl_server.handle_request(envelope)
    finally:
        if reset is not None:
            jsonl_server._actor.reset(reset)
    return _json(200, response)


# --- bearer-auth middleware -----------------------------------------------


class BearerMiddleware(BaseHTTPMiddleware):
    """Constant-time bearer-token gate with multi-token accept-list.

    Matches `Authorization: Bearer <token>` against every entry in the
    accept-list with `hmac.compare_digest` so a side-channel on the matcher
    can't be turned into a token-length / token-bytes oracle. An empty
    accept-list disables the gate (development default).

    Public paths (`/healthz`, `/health`, `/capabilities`) always pass — they
    are intentional information-leakage surfaces (liveness + capability
    advertisement) and there is no benefit to gating them.
    """

    def __init__(self, app: ASGIApp, *, accepted: Iterable[str] = ()) -> None:
        super().__init__(app)
        self._accepted: tuple[str, ...] = tuple(t for t in accepted if t)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not self._accepted:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        # Constant-time match across every accepted token. We can't short-circuit
        # on first mismatch without leaking which slot was hit; OR all results.
        provided = request.headers.get("authorization", "")
        ok = False
        for tok in self._accepted:
            ok = hmac.compare_digest(provided, f"Bearer {tok}") or ok
        if not ok:
            return _json(401, _error_payload(
                "unauthorized", "missing or invalid bearer token",
            ))
        return await call_next(request)


# --- ASGI app builder -----------------------------------------------------


def _attach_vouch_mcp_routes(routes: list, fastmcp_app: Starlette) -> ASGIApp:
    """Take FastMCP's streamable_http_app and graft its `/mcp` route onto our
    outer Starlette so vouch's custom routes (and bearer middleware) share
    one process and one URL space.

    Use ``Route`` (not ``Mount``) so the path is exact -- ``Mount("/mcp", ...)``
    issues a 307 to ``/mcp/`` on a slash-less request, which Claude.ai's
    connector validator follows but breaks naive POSTs. The inner
    ``StreamableHTTPASGIApp`` is reused at both ``/mcp`` and the historical
    ``/messages`` alias.
    """
    inner: ASGIApp | None = None
    for r in fastmcp_app.routes:
        if isinstance(r, Route) and r.path == "/mcp":
            inner = r.app
            routes.append(Route("/mcp", endpoint=inner, methods=["GET", "POST", "DELETE"]))
            routes.append(Route("/messages", endpoint=inner, methods=["GET", "POST", "DELETE"]))
            break
    if inner is None:
        raise RuntimeError(
            "FastMCP.streamable_http_app() did not expose a /mcp route -- "
            "the SDK API has changed; vouch's HTTP transport needs updating."
        )
    return inner


def make_app(
    *,
    token: str | None = None,
    tokens: Iterable[str] = (),
) -> Starlette:
    """Build the Starlette ASGI app.

    ``token`` and ``tokens`` are merged into a single accept-list. Passing
    neither disables the bearer gate (the development default; the bind-policy
    in :func:`make_server` still refuses non-loopback hosts without one).
    """
    # Late-bound import: pulls in 44 kb.* tool registrations. Doing it inside
    # the builder lets unit tests for ServeConfig run without paying for the
    # full server.py import chain.
    from . import server as vouch_server

    accepted: list[str] = []
    if token:
        accepted.append(token)
    accepted.extend(t for t in tokens if t)

    # FastMCP's session manager is single-use per instance: once .run() has
    # exited, the cached _session_manager can't be re-driven. Production never
    # rebuilds the app, but tests that spin servers up and down would hit
    # "called once per instance" on the second build. Reset before each app
    # build so every server gets a fresh, runnable manager.
    #
    # Also force stateless + json-response mode. Stateful Streamable-HTTP
    # requires the client to thread an Mcp-Session-Id header through every
    # follow-up call, which Claude.ai's Custom Connector can do but many
    # lightweight MCP clients (curl-based test packs, scripted integrations)
    # cannot. vouch's kb.* surface has no per-session state to maintain --
    # every call is independent against the same KB on disk -- so dropping
    # the session requirement is a strict UX win with no semantic cost.
    vouch_server.mcp._session_manager = None  # type: ignore[attr-defined]
    vouch_server.mcp.settings.stateless_http = True
    vouch_server.mcp.settings.json_response = True
    fastmcp_app = vouch_server.mcp.streamable_http_app()

    routes: list = [
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/health", _healthz, methods=["GET"]),
        Route("/capabilities", _capabilities, methods=["GET"]),
        Route("/rpc", _rpc, methods=["POST"]),
    ]
    _attach_vouch_mcp_routes(routes, fastmcp_app)

    session_manager = vouch_server.mcp._session_manager  # set by streamable_http_app
    if session_manager is None:
        # Defence-in-depth: streamable_http_app() lazily constructs the
        # manager and we just reset it above, so it must exist now. Crash
        # cleanly rather than booting a half-wired server.
        raise RuntimeError("FastMCP session manager not constructed")

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        # FastMCP's StreamableHTTPSessionManager owns an anyio task group --
        # it can only operate while .run() is active. The Starlette lifespan
        # is the right place to hold it open for the duration of the server.
        async with session_manager.run():
            yield

    return Starlette(
        routes=routes,
        middleware=[Middleware(BearerMiddleware, accepted=accepted)],
        lifespan=_lifespan,
    )


# --- uvicorn wrapper that quacks like a stdlib HTTP server ----------------


class _UvicornServerHandle:
    """Adapter so existing tests (and `vouch serve` itself) can use the
    familiar `serve_forever() / shutdown() / server_close() / server_address`
    quartet, regardless of uvicorn's internal Server lifecycle.

    Threading model: uvicorn's `Server.run()` is sync-blocking, so the
    fixture that calls `serve_forever()` in a thread continues to work.
    Internally uvicorn manages an asyncio loop in that thread.
    """

    def __init__(self, app: ASGIApp, host: str, port: int, *, token_count: int):
        self._app = app
        self._host = host
        self._requested_port = port
        self._token_count = token_count  # for debug only
        self._server: uvicorn.Server | None = None
        self._actual_port: int | None = None
        self._bind_socket: socket.socket | None = None
        # Pre-bind so we can satisfy `server_address` *before* serve_forever
        # is called, matching the stdlib ThreadingHTTPServer behaviour the
        # test fixture relies on (it reads server_address right after
        # make_server returns).
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.listen(128)
        except OSError:
            s.close()
            raise
        self._bind_socket = s
        self._actual_port = s.getsockname()[1]

    @property
    def server_address(self) -> tuple[str, int]:
        return (self._host, self._actual_port or self._requested_port)

    def serve_forever(self) -> None:
        if self._bind_socket is None:
            raise RuntimeError("server already closed")
        config = uvicorn.Config(
            self._app,
            fd=self._bind_socket.fileno(),  # reuse our pre-bound socket
            log_level="warning",
            access_log=False,
            lifespan="on",
            # Limit the read window per request -- a slow drip-feed client
            # mustn't park a worker forever, especially with --allow-public on.
            timeout_keep_alive=30,
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            # Give the server a tick to notice the flag and drain.
            for _ in range(50):
                if not self._server.started:
                    break
                time.sleep(0.05)

    def server_close(self) -> None:
        # uvicorn closes its socket when the run loop exits; our pre-bound
        # socket was duped in via fd= so we close ours too as defence in depth.
        if self._bind_socket is not None:
            with contextlib.suppress(OSError):
                self._bind_socket.close()
            self._bind_socket = None


# Aliases preserved from PR #104's API surface so existing imports still work.
_VouchHTTPServer = _UvicornServerHandle


def make_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    token: str | None = None,
    tokens: Iterable[str] = (),
    allow_public: bool = False,
) -> _UvicornServerHandle:
    """Build (but don't start) the HTTP server, enforcing the bind policy."""
    accepted = list(tokens) + ([token] if token else [])
    if host not in _LOOPBACK_HOSTS and not (allow_public and accepted):
        raise RuntimeError(
            f"refusing to bind non-loopback host {host!r} without both "
            "--allow-public and at least one bearer token "
            "(set VOUCH_HTTP_TOKEN / pass --token / use config.yaml serve.bearer_tokens)"
        )
    app = make_app(token=token, tokens=tokens)
    return _UvicornServerHandle(app, host, port, token_count=len(accepted))


def run_http(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    token: str | None = None,
    tokens: Iterable[str] = (),
    allow_public: bool = False,
) -> None:
    """Serve the kb.* surface over HTTP until interrupted (SIGINT)."""
    server = make_server(host, port, token=token, tokens=tokens, allow_public=allow_public)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# Re-export the legacy _Handler name as None so anything that imports it for
# isinstance-style checks doesn't crash -- the stdlib request-handler model
# isn't used anymore in the Starlette/uvicorn path.
_Handler: type | None = None

# Suppress unused-warning for the threading import: kept in case downstream
# code still references it from the module namespace.
_ = threading

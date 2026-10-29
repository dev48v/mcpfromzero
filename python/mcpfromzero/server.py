"""The server core: dispatch, plus L1 `server/discover`.

The whole of the lifecycle that `initialize` used to run now lives in two
places: `_meta` on every request, and this one optional RPC.
"""

from __future__ import annotations

import sys
from typing import Any, Callable

from .jsonrpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    RequestContext,
    RpcError,
    decode,
    encode,
    error_response,
    is_notification,
    is_request,
    result_response,
    validate_envelope,
)
from .tools import ToolRegistry

SERVER_INFO = {"name": "mcpfromzero", "title": "MCP from Zero", "version": "0.1.0"}


class Server:
    """A minimal, stateless MCP server.

    Stateless is not a style choice here. The 2026-07-28 revision removed
    protocol-level sessions outright, so there is deliberately nowhere on this
    object to stash per-client state - if a level needs continuity it has to
    mint an explicit handle and pass it back as an ordinary argument.
    """

    def __init__(self, name: str = "mcpfromzero"):
        self.name = name
        self._handlers: dict[str, Callable[[Any, RequestContext], dict]] = {}
        self._capabilities: dict = {}
        self._instructions: str | None = None
        self.tools: ToolRegistry | None = None
        self.register("server/discover", self._discover)

    def mount_tools(self, registry: ToolRegistry) -> None:
        """Wire L2 in.

        A capability is a PROMISE, so it is declared here rather than hardcoded:
        a server that advertises `tools` and then answers -32601 has lied, and a
        server that answers `tools/list` without advertising it has made the
        capability meaningless. The two have to move together, which is why
        mounting does both.
        """
        self.tools = registry
        self.capability("tools", {"listChanged": False})
        self.register("tools/list", lambda params, ctx: registry.list(params))
        self.register("tools/call", lambda params, ctx: registry.call(params))

    # -------------------------------------------------------------- wiring
    def register(self, method: str, handler: Callable[[Any, RequestContext], dict]) -> None:
        if method in self._handlers:
            raise ValueError(f"{method} is already registered")
        self._handlers[method] = handler

    def capability(self, name: str, value: Any) -> None:
        self._capabilities[name] = value

    def instructions(self, text: str) -> None:
        self._instructions = text

    # ------------------------------------------------------- L1: discovery
    def _discover(self, params: Any, ctx: RequestContext) -> dict:
        """`server/discover` - what `initialize` used to do, minus the state.

        Three differences from the handshake it replaced, all of which change
        how a client must be written:

        1. It is OPTIONAL for the client. A client MAY skip it entirely and go
           straight to `tools/list`, so nothing may depend on having seen it.
        2. Version negotiation is INVERTED. The server advertises a list and
           the client picks, where before the client proposed and the server
           accepted or countered.
        3. The result is CACHEABLE - `ttlMs` is a freshness hint, so a client
           is entitled to not ask again for that long.
        """
        result: dict = {
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": dict(self._capabilities),
            "ttlMs": 300_000,
            "cacheScope": "public",
        }
        if self._instructions:
            result["instructions"] = self._instructions
        return result

    # ------------------------------------------------------------ dispatch
    def handle(self, raw: str) -> dict | None:
        """One line in, at most one line out.

        Returns None where the protocol requires silence: a notification, or a
        message so malformed that there is no id to answer to.
        """
        req_id: Any = None
        try:
            msg = decode(raw)
            # A parse error has no id to answer, so JSON-RPC says reply with a
            # null id rather than staying silent.
            if isinstance(msg, dict):
                req_id = msg.get("id")
            validate_envelope(msg)

            if is_notification(msg):
                handler = self._handlers.get(msg["method"])
                if handler is not None:
                    ctx = RequestContext.from_params(msg.get("params"))
                    handler(msg.get("params"), ctx)
                return None                      # never answer a notification

            if not is_request(msg):
                raise RpcError(INVALID_REQUEST, "Not a request or a notification")

            method = msg["method"]
            handler = self._handlers.get(method)
            if handler is None:
                raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

            ctx = RequestContext.from_params(msg.get("params"))
            ctx.require_version(SUPPORTED_PROTOCOL_VERSIONS)
            result = handler(msg.get("params"), ctx)
            return result_response(req_id, result, SERVER_INFO)

        except RpcError as e:
            return error_response(req_id, e)
        except Exception as e:                   # noqa: BLE001 - deliberate
            # A handler's internals are not the client's business.
            return error_response(req_id, RpcError(INTERNAL_ERROR, "Internal error",
                                                   {"type": type(e).__name__}))

    # ---------------------------------------------------------------- loop
    def serve_stdio(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            reply = self.handle(line)
            if reply is None:
                continue
            stdout.write(encode(reply) + "\n")
            stdout.flush()

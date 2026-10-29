"""L0 - the wire.

JSON-RPC 2.0 framing, and the one thing about MCP's stdio transport that
everybody gets wrong at least once:

    MCP stdio is NEWLINE-DELIMITED JSON.

It is not LSP. There is no `Content-Length` header. A message is one line, and
a message MUST NOT contain an embedded newline. If you have written a Language
Server before, your fingers will type the header framing and nothing will ever
reply to you.

The second thing this module exists to pin down is that as of protocol revision
2026-07-28 **MCP is stateless**. There is no `initialize`. There is no
`notifications/initialized`. Every single request carries its own protocol
version and client capabilities in `_meta`, under reverse-DNS keys. A server
that remembers anything between requests is a server that will break the moment
it sits behind a load balancer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# error codes
#
# The 2026-07-28 revision added an allocation policy: -32000..-32019 stays
# implementation-defined, -32020..-32099 belongs to the MCP specification. The
# three MCP codes were RENUMBERED into that band in this revision, so code you
# find in older tutorials uses the wrong numbers.
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

HEADER_MISMATCH = -32020                    # was -32001 in the draft
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021  # was -32003
UNSUPPORTED_PROTOCOL_VERSION = -32022        # was -32004

# ---------------------------------------------------------------------------
# the reverse-DNS _meta keys that carry what the handshake used to
# ---------------------------------------------------------------------------
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"

LATEST_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28"]


class RpcError(Exception):
    """An error that is meant to become a JSON-RPC error response.

    Anything else escaping a handler is a bug in the server and becomes
    INTERNAL_ERROR with no detail - a handler's stack trace is not the client's
    business.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


# ---------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------

def encode(message: dict) -> str:
    """One message, one line.

    The newline check is not paranoia. `json.dumps` escapes newlines inside
    strings, so the only way one arrives here is if a caller hands us
    pre-serialised text - and that silently corrupts the stream for every
    message after it.
    """
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    if "\n" in line or "\r" in line:
        raise ValueError("a framed message must not contain a newline")
    return line


def decode(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise RpcError(PARSE_ERROR, "Parse error", {"detail": str(e)}) from e


# ---------------------------------------------------------------------------
# message shapes
# ---------------------------------------------------------------------------

def is_request(msg: Any) -> bool:
    """A request has an id. A notification does not.

    `id: null` is explicitly NOT a request - JSON-RPC 2.0 reserves a null id for
    responses to messages whose id could not be determined. Treating it as a
    request is a real bug: it makes the server answer something that must never
    be answered.
    """
    return isinstance(msg, dict) and "method" in msg and "id" in msg and msg["id"] is not None


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and ("id" not in msg or msg["id"] is None)


def validate_envelope(msg: Any) -> None:
    if not isinstance(msg, dict):
        raise RpcError(INVALID_REQUEST, "A JSON-RPC message must be an object")
    if msg.get("jsonrpc") != "2.0":
        raise RpcError(INVALID_REQUEST, 'The "jsonrpc" field must be exactly "2.0"')
    if "method" not in msg:
        raise RpcError(INVALID_REQUEST, "A request or notification needs a method")
    if not isinstance(msg["method"], str) or not msg["method"]:
        raise RpcError(INVALID_REQUEST, "A method must be a non-empty string")
    if "id" in msg and msg["id"] is not None and not isinstance(msg["id"], (str, int)):
        raise RpcError(INVALID_REQUEST, "An id must be a string or a number")
    if isinstance(msg.get("id"), bool):
        raise RpcError(INVALID_REQUEST, "An id must be a string or a number, not a boolean")
    if "params" in msg and not isinstance(msg["params"], (dict, list)):
        raise RpcError(INVALID_REQUEST, "params must be an object or an array")


def result_response(req_id: Any, result: dict, server_info: dict | None = None) -> dict:
    """Wrap a result.

    Two things this revision requires that older servers do not send:
      * every result carries `resultType` - "complete" or "input_required"
      * the server SHOULD identify itself in the result's `_meta`
    A client reading an older server must treat a missing `resultType` as
    "complete", which is why it is defaulted rather than demanded here.
    """
    body = dict(result)
    body.setdefault("resultType", "complete")
    if server_info is not None:
        meta = dict(body.get("_meta") or {})
        meta.setdefault(META_SERVER_INFO, server_info)
        body["_meta"] = meta
    return {"jsonrpc": "2.0", "id": req_id, "result": body}


def error_response(req_id: Any, err: RpcError) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": err.to_dict()}


# ---------------------------------------------------------------------------
# the per-request context that replaced the handshake
# ---------------------------------------------------------------------------

@dataclass
class RequestContext:
    """Everything `initialize` used to establish, carried on this one request."""

    protocol_version: str | None = None
    client_capabilities: dict = field(default_factory=dict)
    client_info: dict | None = None
    log_level: str | None = None

    @classmethod
    def from_params(cls, params: Any) -> "RequestContext":
        meta = {}
        if isinstance(params, dict):
            meta = params.get("_meta") or {}
            if not isinstance(meta, dict):
                raise RpcError(INVALID_PARAMS, "_meta must be an object")
        return cls(
            protocol_version=meta.get(META_PROTOCOL_VERSION),
            client_capabilities=meta.get(META_CLIENT_CAPABILITIES) or {},
            client_info=meta.get(META_CLIENT_INFO),
            log_level=meta.get(META_LOG_LEVEL),
        )

    def require_version(self, supported: list[str]) -> None:
        """A missing version is NOT an error.

        The spec keeps backward compatibility with servers and clients from
        before the stateless revision, and `server/discover` doubles as the
        probe for it. Rejecting a request that simply omits the field would
        break every older client on contact.
        """
        if self.protocol_version is None:
            return
        if self.protocol_version not in supported:
            raise RpcError(
                UNSUPPORTED_PROTOCOL_VERSION,
                f"Unsupported protocol version: {self.protocol_version}",
                {"supported": supported, "requested": self.protocol_version},
            )

    def require_client_capability(self, path: str) -> None:
        """Walk a dotted path through the declared client capabilities."""
        node: Any = self.client_capabilities
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise RpcError(
                    MISSING_REQUIRED_CLIENT_CAPABILITY,
                    f"This request requires the client capability: {path}",
                    {"capability": path},
                )
            node = node[part]

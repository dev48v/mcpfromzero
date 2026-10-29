"""L2 - tools.

`tools/list` and `tools/call`: the two RPCs almost every MCP server exists to
serve. Four things about them changed in 2026-07-28, and all four break code
copied from an older tutorial:

1. `ListToolsResult` now extends `CacheableResult`, so **`ttlMs` and
   `cacheScope` are REQUIRED**. A server that returns a bare `{"tools": [...]}`
   is not conformant any more. (changelog item 5 / SEP-2549)

2. Servers **SHOULD return tools in a deterministic order**, because clients
   cache the list and an unstable order destroys the LLM's prompt cache.
   (changelog item 3)

3. The tool list no longer varies per connection - sessions are gone, so there
   is no "this client's tools". (changelog item 1 / SEP-2567)

4. A tool that *fails* does NOT produce a JSON-RPC error. It produces a normal
   result with `isError: true`, so the model can see the failure and correct
   itself. Only failures to *find* the tool are protocol errors.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .jsonrpc import INVALID_PARAMS, RpcError

# A page is small on purpose: the conformance suite has to be able to reach a
# second page, and a tutorial that never paginates teaches a server that falls
# over at 200 tools.
DEFAULT_PAGE_SIZE = 2

# tools/list is the same for every caller - no sessions, no user-specific data -
# so a shared proxy is allowed to cache it.
TOOLS_TTL_MS = 60_000
TOOLS_CACHE_SCOPE = "public"


@dataclass
class Tool:
    """One tool, as `tools/list` will describe it.

    `input_schema` is JSON Schema 2020-12 and the root MUST be
    `{"type": "object"}` - tool arguments are always a JSON object, never a
    bare array or string.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    title: str | None = None
    output_schema: dict | None = None
    annotations: dict = field(default_factory=dict)

    def describe(self) -> dict:
        """The wire form. Keys the server has nothing to say about are omitted
        rather than sent as null - an explicit null is a value, and a client
        validating the schema will reject it."""
        d: dict = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            d["title"] = self.title
        if self.output_schema is not None:
            d["outputSchema"] = self.output_schema
        if self.annotations:
            d["annotations"] = self.annotations
        return d


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #

_JSON_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _type_of(value: Any) -> str:
    # bool is a subclass of int in Python, so it has to be tested first or
    # `true` validates as an integer.
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def validate_arguments(schema: dict, args: dict, path: str = "arguments") -> None:
    """A deliberately small JSON Schema subset: type, required, properties,
    enum, minimum/maximum, additionalProperties.

    This is NOT a JSON Schema implementation and does not pretend to be. The
    point of writing it by hand is that it makes visible what a server actually
    owes a caller - a clear message naming the offending field - which is the
    part an off-the-shelf validator usually gets wrong by emitting a stack of
    sub-schema failures no model can act on.
    """
    want = schema.get("type")
    if want is not None:
        got = _type_of(args)
        # JSON Schema says an integer is an acceptable number. The converse is
        # not true, and 1.0 is NOT an integer here.
        ok = got == want or (want == "number" and got == "integer")
        if not ok:
            raise RpcError(INVALID_PARAMS, f"{path} must be of type {want}, got {got}",
                           {"path": path, "expected": want, "actual": got})

    if want == "object" or isinstance(args, dict):
        props: dict = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in args:
                raise RpcError(INVALID_PARAMS, f"{path}.{key} is required",
                               {"path": f"{path}.{key}"})
        if schema.get("additionalProperties") is False:
            for key in args:
                if key not in props:
                    raise RpcError(INVALID_PARAMS, f"{path}.{key} is not a known argument",
                                   {"path": f"{path}.{key}",
                                    "known": sorted(props)})
        for key, sub in props.items():
            if key in args:
                validate_arguments(sub, args[key], f"{path}.{key}")
        return

    if "enum" in schema and args not in schema["enum"]:
        raise RpcError(INVALID_PARAMS, f"{path} must be one of {schema['enum']}",
                       {"path": path, "allowed": schema["enum"]})

    if isinstance(args, (int, float)) and not isinstance(args, bool):
        if "minimum" in schema and args < schema["minimum"]:
            raise RpcError(INVALID_PARAMS, f"{path} must be >= {schema['minimum']}",
                           {"path": path})
        if "maximum" in schema and args > schema["maximum"]:
            raise RpcError(INVALID_PARAMS, f"{path} must be <= {schema['maximum']}",
                           {"path": path})

    if isinstance(args, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(args):
            validate_arguments(schema["items"], item, f"{path}[{i}]")


# --------------------------------------------------------------------------- #
# the cursor
# --------------------------------------------------------------------------- #

_CURSOR_PREFIX = "after:"


def encode_cursor(after_name: str) -> str:
    """An opaque token meaning "resume after this tool".

    It is deliberately NOT an offset. An offset cursor silently skips or
    repeats entries whenever the list changes between pages, and the list is
    allowed to change - that is the whole reason `notifications/tools/
    list_changed` exists. Keying on the last name seen makes a page boundary
    stable under insertion and deletion.

    base64 is here only to make it *look* opaque, so that nobody builds a
    client that parses it. The spec says a cursor is opaque; a client that
    decodes one is a client that will break.
    """
    raw = (_CURSOR_PREFIX + after_name).encode("utf-8")
    # Strip the padding. RFC 4648 section 5 makes `=` optional for base64url
    # and the convention is to omit it - which is what Node's "base64url"
    # encoder does. Leave it on and a cursor minted by this server is one the
    # TypeScript half mints differently for the same page, so a client that
    # cached one and replayed it against the other gets a different answer.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> str:
    try:
        # Python's decoder demands padding and raises "Incorrect padding"
        # without it, so a cursor from the TypeScript half would come back
        # -32602 Invalid cursor. Accept BOTH forms: be strict in what you send,
        # liberal in what you accept.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise RpcError(INVALID_PARAMS, "Invalid cursor", {"cursor": cursor}) from e
    if not raw.startswith(_CURSOR_PREFIX):
        raise RpcError(INVALID_PARAMS, "Invalid cursor", {"cursor": cursor})
    return raw[len(_CURSOR_PREFIX):]


# --------------------------------------------------------------------------- #
# content blocks
# --------------------------------------------------------------------------- #

def text_content(text: str) -> dict:
    return {"type": "text", "text": text}


def format_number(value: float | int) -> str:
    """Render a number the way the TypeScript half renders it.

    Found by the conformance suite, not by reading: `6 / 3` is `2.0` in Python
    and `2` in JavaScript, so the same tool called with the same arguments put
    different text in front of the model depending on which implementation was
    running. For a `content` block that text IS the answer - it is what the
    model reads and reasons over - so "close enough" is not a thing here.

    Both languages already agree on non-integral values, because both print the
    shortest string that round-trips. The only disagreement is the trailing
    `.0`, and Python is the side that has to give it up.
    """
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return str(value)


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

class ToolRegistry:
    """Holds the tools and answers the two tool RPCs.

    There is no per-client state here and there is nowhere to put any. Sessions
    were removed in this revision; a tool needing continuity across calls must
    mint an explicit handle and take it back as an ordinary argument.
    """

    def __init__(self, page_size: int = DEFAULT_PAGE_SIZE):
        self._tools: dict[str, Tool] = {}
        self.page_size = page_size

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"a tool named {tool.name!r} is already registered")
        if tool.input_schema.get("type") != "object":
            # Caught here rather than at call time: an inputSchema whose root is
            # not an object is not merely unusual, it is unrepresentable - there
            # is no way to send those arguments.
            raise ValueError(f"{tool.name}: inputSchema root must be type object")
        self._tools[tool.name] = tool

    # ----------------------------------------------------------- tools/list
    def list(self, params: Any) -> dict:
        """Deterministic order, a page at a time, with the cache hints the
        revision now requires."""
        if params is not None and not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        params = params or {}

        # Sorted by name, every time. Not insertion order, not dict order: an
        # unstable list is a cache the client can never trust, and the
        # changelog asks for determinism precisely so clients can cache.
        names = sorted(self._tools)

        cursor = params.get("cursor")
        if cursor is not None:
            if not isinstance(cursor, str):
                raise RpcError(INVALID_PARAMS, "cursor must be a string")
            after = decode_cursor(cursor)
            # A cursor naming a tool that has since been removed is still
            # usable: resume at the first name that sorts after it, rather than
            # failing a client that did nothing wrong.
            start = next((i for i, n in enumerate(names) if n > after), len(names))
        else:
            start = 0

        page = names[start:start + self.page_size]
        result: dict = {
            "tools": [self._tools[n].describe() for n in page],
            "ttlMs": TOOLS_TTL_MS,
            "cacheScope": TOOLS_CACHE_SCOPE,
        }
        # nextCursor is present only when there is more. Sending one for the
        # last page makes a client loop forever.
        if start + self.page_size < len(names):
            result["nextCursor"] = encode_cursor(page[-1])
        return result

    # ----------------------------------------------------------- tools/call
    def call(self, params: Any) -> dict:
        """Dispatch one tool call.

        The line this method exists to draw:

          * the tool cannot be FOUND, or its arguments do not fit the declared
            schema  ->  a JSON-RPC error (-32602). The model cannot fix this;
            it is a client bug.
          * the tool RAN and failed  ->  a normal result with `isError: true`.
            The model MUST see this, because seeing it is how it retries with
            different arguments.

        Return a protocol error for the second case and the model is told
        nothing; it sees a transport failure and gives up.
        """
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "params.name must be a non-empty string")

        tool = self._tools.get(name)
        if tool is None:
            # -32602, not -32601. METHOD_NOT_FOUND is about `tools/call`
            # itself; the method was found, its arguments were wrong.
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}",
                           {"name": name, "available": sorted(self._tools)})

        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "params.arguments must be an object")
        validate_arguments(tool.input_schema, args)

        try:
            out = tool.handler(args)
        except Exception as e:                      # noqa: BLE001 - deliberate
            # The tool ran and failed. That is a result, not a protocol error.
            return {
                "content": [text_content(f"{type(e).__name__}: {e}")],
                "isError": True,
            }

        if isinstance(out, dict) and "content" in out:
            return out
        if isinstance(out, str):
            return {"content": [text_content(out)]}
        # A tool with an outputSchema owes the client BOTH: structuredContent
        # for the program and a text rendering for the model, which cannot read
        # structuredContent in a prompt without one.
        # json.dumps, not repr: repr is Python-shaped, and the TypeScript half
        # has to produce the same text for the same tool.
        return {"content": [text_content(json.dumps(out))], "structuredContent": out}

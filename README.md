# MCP from Zero

**Every MCP tutorial you have read teaches a handshake that was deleted.**

The Model Context Protocol's `2026-07-28` revision removed `initialize`,
`notifications/initialized`, `ping`, sessions, `resources/subscribe` and
server-initiated requests. Almost everything written about MCP — including the
SDK examples most people copy — describes `2025-06-18` or earlier.

This repository is the protocol as it actually is now, taken apart in nine
levels and **built twice**: once in Python, once in TypeScript, against one
specification, with a conformance suite both must pass.

```bash
python conformance/run.py --impl both
```

```
=== python ===
  ok   L0  l0-framing.json        JSON-RPC 2.0 envelope and framing
  ok   L0  l0-notifications.json  A notification is never answered
  ok   L0  l0-parse-error.json    Unparseable input answers with a null id
  ok   L1  l1-discover.json       server/discover replaces the initialize handshake
  -> 19 steps passed, 0 failed
=== typescript ===
  ... 19 steps passed, 0 failed

CONFORMANT - 0 failing steps
```

---

## What changed, and why it matters

| Gone | Replaced by |
|---|---|
| `initialize` handshake | **`server/discover`** — optional, and **cacheable** via `ttlMs` |
| `notifications/initialized` | nothing. There is no handshake-complete signal |
| Client proposes a version | **Inverted** — the server advertises `supportedVersions[]`, the client picks |
| Protocol sessions, `Mcp-Session-Id` | nothing. Cross-call state is a **server-minted handle passed as an ordinary tool argument** |
| `resources/subscribe` / `unsubscribe` | **`subscriptions/listen`** — one stream, explicit opt-in, and the server **MUST NOT** send a notification type the client did not request |
| `ping`, `logging/setLevel` | removed. Log level is per-request, in `_meta` |
| Server-initiated `sampling` / `roots` / `elicitation` | **MRTR** — the server returns `resultType: "input_required"` and the client *retries* with the answers |

The single biggest consequence: **MCP is stateless now.** Every request carries
its own protocol version and client capabilities in `_meta`, under reverse-DNS
keys:

```json
{
  "jsonrpc": "2.0", "id": 1, "method": "tools/list",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": { "sampling": {} }
    }
  }
}
```

A server that remembers anything between requests will break the first time it
sits behind a load balancer.

## 🔴 A contradiction in the published spec

The schema and the changelog disagree about whether `server/discover` is
mandatory:

> `schema.ts`: *"Servers **SHOULD** implement `server/discover`."*
> `changelog.mdx`: *"servers **MUST** implement this RPC."*

Both are in the same published revision. This repository implements it as
mandatory — the stricter reading — and the conformance suite requires it, on
the grounds that a client is entitled to probe with it.

## The nine levels

| # | Level | What it pins down |
|---|---|---|
| **L0** | The wire | JSON-RPC 2.0, newline-delimited stdio, and why there is no handshake |
| L1 | Discovery | `server/discover`, inverted version negotiation, caching |
| L2 | Tools | `tools/list`, `tools/call`, schemas, deterministic ordering |
| L3 | Resources | `resources/list`, `read`, `templates/list` |
| L4 | Prompts | `prompts/list`, `prompts/get`, `completion/complete` |
| L5 | Subscriptions | `subscriptions/listen` and the MUST-NOT rule |
| L6 | MRTR | `input_required`, `inputRequests`, retry semantics |
| L7 | Security | untrusted servers, consent, the renumbered error codes |
| L8 | Production | cancellation, progress, logging, caching, extensions |

L0 and L1 are built. The rest follow.

## Why two implementations

A test written in the same language as the thing it tests inherits its author's
reading of the spec. Two implementations passing one external suite is the only
way to find out that the *reading itself* was wrong.

The conformance cases are language-neutral JSON — a sequence of messages to
send and expectations about what comes back — and the runner drives either
implementation over stdio. Neither language's code is consulted when writing a
case; only the specification is.

## 🔴 Three things that cost time

**MCP stdio is newline-delimited JSON.** It is not LSP. There is no
`Content-Length` header. If you have written a Language Server before, your
fingers will type the header framing and nothing will ever reply to you.

**`id: null` is not a request.** JSON-RPC reserves a null id for replies to
messages whose id could not be determined. Treat it as a request and your
server answers something that must never be answered. The suite checks this.

**`tsc` writes output even when it reports errors.** The first TypeScript build
here emitted four type errors *and produced a working `dist/`*. `noEmitOnError`
is off by default. It is on in `tsconfig.json` now, and the guard is tested by
deliberately breaking a type from a clean tree and confirming nothing is
written.

## Layout

```
conformance/
  run.py              the runner - spawns an implementation, talks stdio
  cases/*.json        language-neutral cases, shared by both languages
python/
  mcpfromzero/        jsonrpc.py, server.py
typescript/
  src/                jsonrpc.ts, server.ts
docs/
  schema-2026-07-28.ts          the upstream schema, vendored for reference
  upstream-changelog-*.mdx      the upstream changelog
```

## Running it

```bash
git clone https://github.com/dev48v/mcpfromzero
cd mcpfromzero

# Python side needs nothing installed
python conformance/run.py --impl python

# TypeScript side
cd typescript && npm install && npm run build && cd ..
python conformance/run.py --impl both
```

Python 3.10+, Node 18+.

## What this is not

It is **not an SDK**. Use the official ones to ship something. This is a
teardown: the smallest correct implementation of each part of the protocol,
with the reasoning left in, so that reading it teaches you the protocol rather
than teaching you a library.

---

MIT © Devanshu Biswas. Protocol revision `2026-07-28`.

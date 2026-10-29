# Vendored upstream files

The two files beside this notice are **not my work**. They are copied
unmodified from the Model Context Protocol specification so that every claim
in this repository can be checked against the text it was built from, without
a network round trip and without the risk of the upstream text moving under a
reader's feet.

| File | Upstream |
|---|---|
| `schema-2026-07-28.ts` | [`modelcontextprotocol/modelcontextprotocol`](https://github.com/modelcontextprotocol/modelcontextprotocol) — `schema/2026-07-28/schema.ts` |
| `upstream-changelog-2026-07-28.mdx` | same repository — the `2026-07-28` changelog |

Both are MIT-licensed, © Anthropic, PBC and the MCP contributors. They are
redistributed here under that licence, which is compatible with this
repository's own MIT licence. The MIT licence in `../LICENSE` and the
copyright line in it cover **my** code — `conformance/`, `python/`,
`typescript/` and `README.md` — not these two files.

If the two ever disagree with the live specification, the live specification
wins and this copy is stale. The revision date is in the filename so that is
easy to check.

Two places where they disagree with *each other* are recorded in the
repository README: whether `server/discover` is SHOULD or MUST, and where the
boundary falls between `-32602` and `isError` for a bad tool argument.

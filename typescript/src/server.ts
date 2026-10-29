/**
 * The server core: dispatch, plus L1 `server/discover`.
 *
 * The whole of the lifecycle that `initialize` used to run now lives in two
 * places: `_meta` on every request, and this one RPC.
 */
import * as readline from "node:readline";
import {
  INTERNAL_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND,
  SUPPORTED_PROTOCOL_VERSIONS, RequestContext, RpcError,
  decode, encode, errorResponse, isNotification, isRequest,
  resultResponse, validateEnvelope,
} from "./jsonrpc.js";
import type { ToolRegistry } from "./tools.js";

export const SERVER_INFO = { name: "mcpfromzero", title: "MCP from Zero", version: "0.1.0" };

type Handler = (params: unknown, ctx: RequestContext) => Record<string, unknown>;

/**
 * A minimal, stateless MCP server.
 *
 * Stateless is not a style choice. The 2026-07-28 revision removed
 * protocol-level sessions outright, so there is deliberately nowhere on this
 * object to stash per-client state - a level needing continuity must mint an
 * explicit handle and take it back as an ordinary argument.
 */
export class Server {
  private handlers = new Map<string, Handler>();
  private caps: Record<string, unknown> = {};
  private instructionsText?: string;

  tools?: ToolRegistry;

  constructor(public name = "mcpfromzero") {
    this.register("server/discover", (p, c) => this.discover(p, c));
  }

  /**
   * Wire L2 in.
   *
   * A capability is a PROMISE, so it is declared here rather than hardcoded. A
   * server that advertises `tools` and then answers -32601 has lied; a server
   * that answers `tools/list` without advertising it has made the capability
   * meaningless. The two must move together, which is why mounting does both.
   */
  mountTools(registry: ToolRegistry): void {
    this.tools = registry;
    this.capability("tools", { listChanged: false });
    this.register("tools/list", (params) => registry.list(params));
    this.register("tools/call", (params) => registry.call(params));
  }

  register(method: string, handler: Handler): void {
    if (this.handlers.has(method)) throw new Error(`${method} is already registered`);
    this.handlers.set(method, handler);
  }

  capability(name: string, value: unknown): void { this.caps[name] = value; }
  instructions(text: string): void { this.instructionsText = text; }

  /**
   * `server/discover` - what `initialize` used to do, minus the state.
   *
   * Three differences from the handshake it replaced:
   *   1. OPTIONAL for the client - it MAY go straight to tools/list, so
   *      nothing may depend on having seen this.
   *   2. Version negotiation is INVERTED - the server advertises a list and
   *      the client picks, where before the client proposed.
   *   3. The result is CACHEABLE - ttlMs is a freshness hint.
   */
  private discover(_params: unknown, _ctx: RequestContext): Record<string, unknown> {
    const result: Record<string, unknown> = {
      supportedVersions: [...SUPPORTED_PROTOCOL_VERSIONS],
      capabilities: { ...this.caps },
      ttlMs: 300000,
      cacheScope: "public",
    };
    if (this.instructionsText) result.instructions = this.instructionsText;
    return result;
  }

  /** One line in, at most one line out. null means the protocol requires silence. */
  handle(raw: string): Record<string, unknown> | null {
    let id: unknown = null;
    try {
      const msg = decode(raw);
      // A parse error has no id to answer, so JSON-RPC says reply with a null id
      // rather than staying silent.
      if (typeof msg === "object" && msg !== null && !Array.isArray(msg)) {
        id = (msg as Record<string, unknown>).id ?? null;
      }
      validateEnvelope(msg);
      const m = msg as Record<string, unknown>;

      if (isNotification(m)) {
        const h = this.handlers.get(m.method as string);
        if (h) h(m.params, RequestContext.fromParams(m.params));
        return null;                       // never answer a notification
      }
      if (!isRequest(m)) throw new RpcError(INVALID_REQUEST, "Not a request or a notification");

      const handler = this.handlers.get(m.method as string);
      if (!handler) throw new RpcError(METHOD_NOT_FOUND, `Method not found: ${m.method}`);

      const ctx = RequestContext.fromParams(m.params);
      ctx.requireVersion(SUPPORTED_PROTOCOL_VERSIONS);
      return resultResponse(id, handler(m.params, ctx), SERVER_INFO);
    } catch (e) {
      if (e instanceof RpcError) return errorResponse(id, e);
      // A handler's internals are not the client's business.
      return errorResponse(id, new RpcError(INTERNAL_ERROR, "Internal error",
        { type: (e as Error)?.constructor?.name ?? "Error" }));
    }
  }

  serveStdio(): void {
    const rl = readline.createInterface({ input: process.stdin, terminal: false });
    rl.on("line", (line) => {
      const t = line.trim();
      if (!t) return;
      const reply = this.handle(t);
      if (reply !== null) process.stdout.write(encode(reply) + "\n");
    });
  }
}

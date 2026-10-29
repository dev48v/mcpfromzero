/**
 * L0 - the wire.
 *
 * JSON-RPC 2.0 framing, and the one thing about MCP's stdio transport that
 * everybody gets wrong at least once:
 *
 *     MCP stdio is NEWLINE-DELIMITED JSON.
 *
 * It is not LSP. There is no `Content-Length` header. A message is one line,
 * and a message MUST NOT contain an embedded newline.
 *
 * The second thing this module pins down is that as of protocol revision
 * 2026-07-28 MCP is **stateless**. There is no `initialize`, no
 * `notifications/initialized`, and no session. Every request carries its own
 * protocol version and client capabilities in `_meta`.
 *
 * This file is written from the specification, not from the Python module next
 * door. Where the two disagree, the conformance suite is what tells us which
 * reading was wrong.
 */

// --------------------------------------------------------------------------
// error codes
//
// 2026-07-28 added an allocation policy: -32000..-32019 stays
// implementation-defined, -32020..-32099 belongs to the MCP specification. The
// three MCP codes were RENUMBERED into that band, so older tutorials use the
// wrong numbers.
// --------------------------------------------------------------------------
export const PARSE_ERROR = -32700;
export const INVALID_REQUEST = -32600;
export const METHOD_NOT_FOUND = -32601;
export const INVALID_PARAMS = -32602;
export const INTERNAL_ERROR = -32603;

export const HEADER_MISMATCH = -32020;                     // was -32001
export const MISSING_REQUIRED_CLIENT_CAPABILITY = -32021;  // was -32003
export const UNSUPPORTED_PROTOCOL_VERSION = -32022;        // was -32004

// --------------------------------------------------------------------------
// the reverse-DNS _meta keys that carry what the handshake used to
// --------------------------------------------------------------------------
export const META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion";
export const META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities";
export const META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo";
export const META_SERVER_INFO = "io.modelcontextprotocol/serverInfo";
export const META_LOG_LEVEL = "io.modelcontextprotocol/logLevel";

export const LATEST_PROTOCOL_VERSION = "2026-07-28";
export const SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28"];

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export class RpcError extends Error {
  code: number;
  data?: unknown;
  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.code = code;
    this.data = data;
  }
  toJSON(): Record<string, unknown> {
    const out: Record<string, unknown> = { code: this.code, message: this.message };
    if (this.data !== undefined) out.data = this.data;
    return out;
  }
}

// --------------------------------------------------------------------------
// framing
// --------------------------------------------------------------------------

export function encode(message: unknown): string {
  const line = JSON.stringify(message);
  // JSON.stringify escapes newlines inside strings, so one only arrives here
  // if a caller hands us pre-serialised text - and that silently corrupts the
  // stream for every message after it.
  if (line.includes("\n") || line.includes("\r")) {
    throw new Error("a framed message must not contain a newline");
  }
  return line;
}

export function decode(line: string): unknown {
  try {
    return JSON.parse(line);
  } catch (e) {
    throw new RpcError(PARSE_ERROR, "Parse error", { detail: String(e) });
  }
}

// --------------------------------------------------------------------------
// message shapes
// --------------------------------------------------------------------------

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * A request has an id. A notification does not.
 *
 * `id: null` is explicitly NOT a request - JSON-RPC 2.0 reserves a null id for
 * responses to messages whose id could not be determined. Treating it as a
 * request makes the server answer something that must never be answered.
 */
export function isRequest(msg: unknown): boolean {
  return isObj(msg) && "method" in msg && "id" in msg && msg.id !== null;
}

export function isNotification(msg: unknown): boolean {
  return isObj(msg) && "method" in msg && (!("id" in msg) || msg.id === null);
}

export function validateEnvelope(msg: unknown): asserts msg is Record<string, unknown> {
  if (!isObj(msg)) throw new RpcError(INVALID_REQUEST, "A JSON-RPC message must be an object");
  if (msg.jsonrpc !== "2.0") throw new RpcError(INVALID_REQUEST, 'The "jsonrpc" field must be exactly "2.0"');
  if (!("method" in msg)) throw new RpcError(INVALID_REQUEST, "A request or notification needs a method");
  if (typeof msg.method !== "string" || msg.method === "") {
    throw new RpcError(INVALID_REQUEST, "A method must be a non-empty string");
  }
  if ("id" in msg && msg.id !== null) {
    // typeof true === "boolean", so this also rejects a boolean id
    if (typeof msg.id !== "string" && typeof msg.id !== "number") {
      throw new RpcError(INVALID_REQUEST, "An id must be a string or a number");
    }
  }
  if ("params" in msg && !isObj(msg.params) && !Array.isArray(msg.params)) {
    throw new RpcError(INVALID_REQUEST, "params must be an object or an array");
  }
}

/**
 * Wrap a result.
 *
 * Two things this revision requires that older servers do not send:
 *   - every result carries `resultType` - "complete" or "input_required"
 *   - the server SHOULD identify itself in the result's `_meta`
 * A client reading an older server must treat a missing `resultType` as
 * "complete", which is why it is defaulted rather than demanded.
 */
export function resultResponse(
  id: unknown, result: Record<string, unknown>, serverInfo?: Record<string, unknown>,
): Record<string, unknown> {
  const body: Record<string, unknown> = { ...result };
  if (body.resultType === undefined) body.resultType = "complete";
  if (serverInfo) {
    const meta: Record<string, unknown> = { ...(body._meta as object ?? {}) };
    if (meta[META_SERVER_INFO] === undefined) meta[META_SERVER_INFO] = serverInfo;
    body._meta = meta;
  }
  return { jsonrpc: "2.0", id, result: body };
}

export function errorResponse(id: unknown, err: RpcError): Record<string, unknown> {
  return { jsonrpc: "2.0", id: id === undefined ? null : id, error: err.toJSON() };
}

// --------------------------------------------------------------------------
// the per-request context that replaced the handshake
// --------------------------------------------------------------------------

export class RequestContext {
  protocolVersion?: string;
  clientCapabilities: Record<string, unknown> = {};
  clientInfo?: Record<string, unknown>;
  logLevel?: string;

  static fromParams(params: unknown): RequestContext {
    const ctx = new RequestContext();
    if (isObj(params) && "_meta" in params) {
      const meta = params._meta;
      if (!isObj(meta)) throw new RpcError(INVALID_PARAMS, "_meta must be an object");
      ctx.protocolVersion = meta[META_PROTOCOL_VERSION] as string | undefined;
      ctx.clientCapabilities = (meta[META_CLIENT_CAPABILITIES] as Record<string, unknown>) ?? {};
      ctx.clientInfo = meta[META_CLIENT_INFO] as Record<string, unknown> | undefined;
      ctx.logLevel = meta[META_LOG_LEVEL] as string | undefined;
    }
    return ctx;
  }

  /**
   * A missing version is NOT an error.
   *
   * The spec keeps backward compatibility with implementations from before the
   * stateless revision, and `server/discover` doubles as the probe for it.
   * Rejecting a request that simply omits the field would break every older
   * client on contact.
   */
  requireVersion(supported: string[]): void {
    if (this.protocolVersion === undefined) return;
    if (!supported.includes(this.protocolVersion)) {
      throw new RpcError(
        UNSUPPORTED_PROTOCOL_VERSION,
        `Unsupported protocol version: ${this.protocolVersion}`,
        { supported, requested: this.protocolVersion },
      );
    }
  }

  requireClientCapability(path: string): void {
    let node: unknown = this.clientCapabilities;
    for (const part of path.split(".")) {
      if (!isObj(node) || !(part in node)) {
        throw new RpcError(
          MISSING_REQUIRED_CLIENT_CAPABILITY,
          `This request requires the client capability: ${path}`,
          { capability: path },
        );
      }
      node = node[part];
    }
  }
}

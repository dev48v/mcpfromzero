/**
 * L2 - tools.
 *
 * `tools/list` and `tools/call`. Four things changed in 2026-07-28 and all
 * four break code copied from an older tutorial:
 *
 * 1. `ListToolsResult` extends `CacheableResult`, so **`ttlMs` and
 *    `cacheScope` are REQUIRED**. A bare `{tools: [...]}` is not conformant.
 * 2. Servers **SHOULD return tools in a deterministic order** - clients cache
 *    the list, and an unstable order destroys the LLM's prompt cache.
 * 3. The tool list no longer varies per connection. Sessions are gone.
 * 4. A tool that FAILS returns a normal result with `isError: true`, not a
 *    JSON-RPC error. Only failing to FIND the tool is a protocol error.
 */
import { INVALID_PARAMS, RpcError } from "./jsonrpc.js";

/** Small on purpose: a tutorial that never paginates teaches a server that
 *  falls over at 200 tools. */
export const DEFAULT_PAGE_SIZE = 2;
export const TOOLS_TTL_MS = 60000;
export const TOOLS_CACHE_SCOPE = "public";

export type ToolHandler = (args: Record<string, unknown>) => unknown;

export interface ToolSpec {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  handler: ToolHandler;
  title?: string;
  outputSchema?: Record<string, unknown>;
  annotations?: Record<string, unknown>;
}

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export function textContent(text: string): Record<string, unknown> {
  return { type: "text", text };
}

/**
 * Render a number the way the Python half renders it.
 *
 * Found by the conformance suite, not by reading: `6 / 3` is `2` here and
 * `2.0` in Python, so the same tool called with the same arguments put
 * different text in front of the model depending on which implementation was
 * running. For a `content` block that text IS the answer.
 *
 * This side needs no work - JavaScript has one number type and already drops
 * the trailing `.0`. The function exists anyway, on both sides, so that the
 * agreement is written down somewhere instead of being an accident that the
 * next edit quietly breaks.
 */
export function formatNumber(value: number): string {
  return String(value);
}

/** The wire form. Absent keys are omitted, never sent as null - an explicit
 *  null is a value, and a client validating the schema will reject it. */
function describe(t: ToolSpec): Record<string, unknown> {
  const d: Record<string, unknown> = {
    name: t.name, description: t.description, inputSchema: t.inputSchema,
  };
  if (t.title) d.title = t.title;
  if (t.outputSchema !== undefined) d.outputSchema = t.outputSchema;
  if (t.annotations && Object.keys(t.annotations).length) d.annotations = t.annotations;
  return d;
}

// --------------------------------------------------------------------------
// argument validation
// --------------------------------------------------------------------------

/**
 * JSON's type names, not JavaScript's. `typeof null === "object"` and
 * `typeof []` is also `"object"`, so neither is usable here, and an integer is
 * a distinct JSON Schema type that JavaScript does not have at all.
 */
function jsonTypeOf(v: unknown): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "number") return Number.isInteger(v) ? "integer" : "number";
  if (typeof v === "string") return "string";
  if (typeof v === "object") return "object";
  return "unknown";
}

/**
 * A deliberately small JSON Schema subset: type, required, properties, enum,
 * minimum/maximum, additionalProperties, items.
 *
 * Not a JSON Schema implementation and not pretending to be. Writing it by
 * hand makes visible what a server owes a caller - one clear message naming
 * the offending field - which is the part an off-the-shelf validator usually
 * gets wrong, emitting a stack of sub-schema failures no model can act on.
 */
export function validateArguments(schema: Record<string, unknown>, value: unknown, path = "arguments"): void {
  const want = schema.type as string | undefined;
  if (want !== undefined) {
    const got = jsonTypeOf(value);
    // JSON Schema says an integer is an acceptable number. The converse is not
    // true - and in JavaScript 1.0 IS an integer, unlike Python.
    const ok = got === want || (want === "number" && got === "integer");
    if (!ok) {
      throw new RpcError(INVALID_PARAMS, `${path} must be of type ${want}, got ${got}`,
        { path, expected: want, actual: got });
    }
  }

  if (want === "object" || isObj(value)) {
    const obj = value as Record<string, unknown>;
    const props = (schema.properties as Record<string, Record<string, unknown>>) ?? {};
    for (const key of (schema.required as string[]) ?? []) {
      // `in` rather than `!== undefined`: an explicitly-sent null satisfies
      // "required" and must fail on type instead, with a better message.
      if (!(key in obj)) {
        throw new RpcError(INVALID_PARAMS, `${path}.${key} is required`, { path: `${path}.${key}` });
      }
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(obj)) {
        if (!(key in props)) {
          throw new RpcError(INVALID_PARAMS, `${path}.${key} is not a known argument`,
            { path: `${path}.${key}`, known: Object.keys(props).sort() });
        }
      }
    }
    for (const [key, sub] of Object.entries(props)) {
      if (key in obj) validateArguments(sub, obj[key], `${path}.${key}`);
    }
    return;
  }

  if (Array.isArray(schema.enum) && !schema.enum.includes(value as never)) {
    throw new RpcError(INVALID_PARAMS, `${path} must be one of ${JSON.stringify(schema.enum)}`,
      { path, allowed: schema.enum });
  }

  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) {
      throw new RpcError(INVALID_PARAMS, `${path} must be >= ${schema.minimum}`, { path });
    }
    if (typeof schema.maximum === "number" && value > schema.maximum) {
      throw new RpcError(INVALID_PARAMS, `${path} must be <= ${schema.maximum}`, { path });
    }
  }

  if (Array.isArray(value) && isObj(schema.items)) {
    value.forEach((item, i) => validateArguments(schema.items as Record<string, unknown>, item, `${path}[${i}]`));
  }
}

// --------------------------------------------------------------------------
// the cursor
// --------------------------------------------------------------------------

const CURSOR_PREFIX = "after:";

/**
 * An opaque token meaning "resume after this tool".
 *
 * Deliberately NOT an offset. An offset cursor silently skips or repeats
 * entries whenever the list changes between pages - and the list is allowed to
 * change, which is the entire reason `notifications/tools/list_changed`
 * exists. Keying on the last name seen keeps a page boundary stable under
 * insertion and deletion.
 *
 * base64 is here only to make it LOOK opaque so nobody writes a client that
 * parses it. The spec says a cursor is opaque; a client that decodes one is a
 * client that will break.
 */
export function encodeCursor(afterName: string): string {
  // "base64url" already omits the `=` padding, which is what RFC 4648 section
  // 5 makes optional and what the Python half now also does. The two must
  // agree: a client that caches a cursor from one and replays it against the
  // other has to get the same page.
  return Buffer.from(CURSOR_PREFIX + afterName, "utf8").toString("base64url");
}

export function decodeCursor(cursor: string): string {
  let raw: string;
  try {
    raw = Buffer.from(cursor, "base64url").toString("utf8");
  } catch {
    throw new RpcError(INVALID_PARAMS, "Invalid cursor", { cursor });
  }
  // Node's decoder accepts the padded form as well, so a cursor minted by the
  // Python half decodes here unchanged. It is also permissive enough never to
  // throw on junk, so the prefix - not the decode - is what actually validates
  // the token.
  if (!raw.startsWith(CURSOR_PREFIX)) {
    throw new RpcError(INVALID_PARAMS, "Invalid cursor", { cursor });
  }
  return raw.slice(CURSOR_PREFIX.length);
}

// --------------------------------------------------------------------------
// the registry
// --------------------------------------------------------------------------

/**
 * Holds the tools and answers the two tool RPCs.
 *
 * No per-client state, and nowhere to put any. Sessions were removed in this
 * revision; a tool needing continuity must mint an explicit handle and take it
 * back as an ordinary argument.
 */
export class ToolRegistry {
  private tools = new Map<string, ToolSpec>();

  constructor(public pageSize = DEFAULT_PAGE_SIZE) {}

  add(tool: ToolSpec): void {
    if (this.tools.has(tool.name)) throw new Error(`a tool named ${tool.name} is already registered`);
    if (tool.inputSchema.type !== "object") {
      // Caught at registration, not at call time: an inputSchema whose root is
      // not an object is unrepresentable - there is no way to send those
      // arguments.
      throw new Error(`${tool.name}: inputSchema root must be type object`);
    }
    this.tools.set(tool.name, tool);
  }

  /** Deterministic order, a page at a time, with the cache hints the revision
   *  now requires. */
  list(params: unknown): Record<string, unknown> {
    if (params !== undefined && params !== null && !isObj(params)) {
      throw new RpcError(INVALID_PARAMS, "params must be an object");
    }
    const p = (params as Record<string, unknown>) ?? {};

    // Sorted by name, every time. A Map preserves insertion order, which is
    // stable but arbitrary - and the changelog asks for determinism precisely
    // so clients can cache, which an arbitrary order defeats.
    const names = [...this.tools.keys()].sort();

    let start = 0;
    const cursor = p.cursor;
    if (cursor !== undefined && cursor !== null) {
      if (typeof cursor !== "string") throw new RpcError(INVALID_PARAMS, "cursor must be a string");
      const after = decodeCursor(cursor);
      // A cursor naming a tool that has since been removed is still usable:
      // resume at the first name sorting after it, rather than failing a
      // client that did nothing wrong.
      const idx = names.findIndex((n) => n > after);
      start = idx === -1 ? names.length : idx;
    }

    const page = names.slice(start, start + this.pageSize);
    const result: Record<string, unknown> = {
      tools: page.map((n) => describe(this.tools.get(n)!)),
      ttlMs: TOOLS_TTL_MS,
      cacheScope: TOOLS_CACHE_SCOPE,
    };
    // nextCursor only when there is more. Sending one for the last page makes
    // a client loop forever.
    if (start + this.pageSize < names.length) result.nextCursor = encodeCursor(page[page.length - 1]);
    return result;
  }

  /**
   * Dispatch one tool call.
   *
   * The line this method exists to draw:
   *
   *   - the tool cannot be FOUND, or its arguments do not fit the declared
   *     schema -> a JSON-RPC error (-32602). The model cannot fix this.
   *   - the tool RAN and failed -> a normal result with `isError: true`. The
   *     model MUST see this, because seeing it is how it retries.
   *
   * Return a protocol error for the second case and the model is told nothing;
   * it sees a transport failure and gives up.
   */
  call(params: unknown): Record<string, unknown> {
    if (!isObj(params)) throw new RpcError(INVALID_PARAMS, "params must be an object");
    const name = params.name;
    if (typeof name !== "string" || name === "") {
      throw new RpcError(INVALID_PARAMS, "params.name must be a non-empty string");
    }
    const tool = this.tools.get(name);
    if (!tool) {
      // -32602, not -32601. METHOD_NOT_FOUND is about `tools/call` itself; the
      // method was found, its arguments were wrong.
      throw new RpcError(INVALID_PARAMS, `Unknown tool: ${name}`,
        { name, available: [...this.tools.keys()].sort() });
    }

    const rawArgs = params.arguments ?? {};
    if (!isObj(rawArgs)) throw new RpcError(INVALID_PARAMS, "params.arguments must be an object");
    validateArguments(tool.inputSchema, rawArgs);

    let out: unknown;
    try {
      out = tool.handler(rawArgs);
    } catch (e) {
      // The tool ran and failed. That is a result, not a protocol error.
      const err = e as Error;
      return {
        content: [textContent(`${err?.constructor?.name ?? "Error"}: ${err?.message ?? String(e)}`)],
        isError: true,
      };
    }

    if (isObj(out) && "content" in out) return out;
    if (typeof out === "string") return { content: [textContent(out)] };
    // A tool with an outputSchema owes the client BOTH: structuredContent for
    // the program and a text rendering for the model, which cannot read
    // structuredContent in a prompt without one.
    return { content: [textContent(JSON.stringify(out))], structuredContent: out };
  }
}

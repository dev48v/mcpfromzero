/**
 * Entry point for the conformance runner: `node dist/serve.js`.
 *
 * The tools here are the fixture the L2 conformance cases are written against,
 * so they must match the Python half name for name and schema for schema:
 *
 *     add       structured output alongside a text rendering
 *     divide    a tool that FAILS - isError, not a protocol error
 *     echo      the simplest possible shape
 *     now       a tool with no arguments at all
 *     slugify   enum + additionalProperties: false
 *
 * Sorted by name that is add, divide, echo, now, slugify - five tools over a
 * page size of two, so `tools/list` paginates three times to deliver them.
 */
import { Server } from "./server.js";
import { ToolRegistry, formatNumber, textContent } from "./tools.js";

export function build(): Server {
  const s = new Server();
  s.instructions(
    "MCP from Zero reference server. Built against protocol revision " +
    "2026-07-28, which is stateless - no initialize handshake."
  );

  const reg = new ToolRegistry();

  reg.add({
    name: "add",
    title: "Add two numbers",
    description: "Add two numbers and return the sum.",
    inputSchema: {
      type: "object",
      properties: { a: { type: "number" }, b: { type: "number" } },
      required: ["a", "b"],
      additionalProperties: false,
    },
    outputSchema: {
      type: "object",
      properties: { sum: { type: "number" } },
      required: ["sum"],
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
    handler: (a) => {
      const sum = (a.a as number) + (a.b as number);
      return { content: [textContent(formatNumber(sum))], structuredContent: { sum } };
    },
  });

  reg.add({
    name: "divide",
    description: "Divide one number by another. Fails on a zero denominator.",
    inputSchema: {
      type: "object",
      properties: { numerator: { type: "number" }, denominator: { type: "number" } },
      required: ["numerator", "denominator"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    handler: (a) => {
      // The zero check is explicit rather than left to the language. This is
      // exactly where the two halves of this repository disagree: Python
      // raises ZeroDivisionError, JavaScript quietly returns Infinity, which
      // then serialises as `null` and reaches the model as a success. A tool
      // whose failure mode depends on the implementation language is a tool
      // whose conformance case cannot be written.
      if ((a.denominator as number) === 0) throw new Error("denominator must not be zero");
      return { content: [textContent(formatNumber((a.numerator as number) / (a.denominator as number)))] };
    },
  });

  reg.add({
    name: "echo",
    description: "Return the text it was given.",
    inputSchema: {
      type: "object",
      properties: { text: { type: "string" } },
      required: ["text"],
    },
    handler: (a) => a.text as string,
  });

  reg.add({
    name: "now",
    description: "A tool that takes no arguments at all.",
    // An empty `properties` is not the same as no schema. The root must still
    // be type object, because tool arguments are always an object.
    inputSchema: { type: "object", properties: {} },
    handler: () => "ok",
  });

  reg.add({
    name: "slugify",
    description: "Lower-case a string and join its words with a separator.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string" },
        separator: { type: "string", enum: ["-", "_"] },
      },
      required: ["text"],
      additionalProperties: false,
    },
    handler: (a) => {
      const sep = (a.separator as string) || "-";
      // `split(/\s+/)` on a string with leading space yields an empty first
      // element, where Python's bare `.split()` drops it. Filter, or the two
      // halves disagree on " Hello World ".
      return (a.text as string).toLowerCase().split(/\s+/).filter(Boolean).join(sep);
    },
  });

  s.mountTools(reg);
  return s;
}

build().serveStdio();

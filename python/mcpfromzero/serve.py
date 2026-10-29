"""Entry point for the conformance runner: `python -m mcpfromzero.serve`.

The tools here are the fixture the L2 conformance cases are written against,
so the TypeScript half must offer the same five under the same names with the
same schemas. They are chosen to cover the things that go wrong:

    add       structured output alongside a text rendering
    divide    a tool that FAILS - isError, not a protocol error
    echo      the simplest possible shape
    now       a tool with no arguments at all
    slugify   enum + additionalProperties: false

Sorted by name that is add, divide, echo, now, slugify - five tools over a page
size of two, so `tools/list` has to paginate three times to deliver them.
"""
from .server import Server
from .tools import Tool, ToolRegistry, format_number, text_content


def build() -> Server:
    s = Server()
    s.instructions(
        "MCP from Zero reference server. Built against protocol revision "
        "2026-07-28, which is stateless - no initialize handshake."
    )

    reg = ToolRegistry()

    reg.add(Tool(
        name="add",
        title="Add two numbers",
        description="Add two numbers and return the sum.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"sum": {"type": "number"}},
            "required": ["sum"],
        },
        annotations={"readOnlyHint": True, "openWorldHint": False},
        handler=lambda a: {
            "content": [text_content(format_number(a["a"] + a["b"]))],
            "structuredContent": {"sum": a["a"] + a["b"]},
        },
    ))

    def _divide(a: dict):
        # The zero check is explicit rather than left to the language, because
        # the two halves of this repository do NOT agree: Python raises
        # ZeroDivisionError, JavaScript quietly returns Infinity. A tool whose
        # failure mode depends on the implementation language is a tool whose
        # conformance case cannot be written.
        #
        # Raising is the point. The registry turns it into isError: true so the
        # model can see what happened and try different arguments.
        if a["denominator"] == 0:
            raise ValueError("denominator must not be zero")
        return {"content": [text_content(format_number(a["numerator"] / a["denominator"]))]}

    reg.add(Tool(
        name="divide",
        description="Divide one number by another. Fails on a zero denominator.",
        input_schema={
            "type": "object",
            "properties": {
                "numerator": {"type": "number"},
                "denominator": {"type": "number"},
            },
            "required": ["numerator", "denominator"],
            "additionalProperties": False,
        },
        annotations={"readOnlyHint": True},
        handler=_divide,
    ))

    reg.add(Tool(
        name="echo",
        description="Return the text it was given.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=lambda a: a["text"],
    ))

    reg.add(Tool(
        name="now",
        description="A tool that takes no arguments at all.",
        # An empty `properties` is not the same as no schema. The root must
        # still be type object, because tool arguments are always an object.
        input_schema={"type": "object", "properties": {}},
        handler=lambda a: "ok",
    ))

    reg.add(Tool(
        name="slugify",
        description="Lower-case a string and join its words with a separator.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "separator": {"type": "string", "enum": ["-", "_"]},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=lambda a: (a.get("separator") or "-").join(a["text"].lower().split()),
    ))

    s.mount_tools(reg)
    return s


if __name__ == "__main__":
    build().serve_stdio()

"""Entry point for the conformance runner: `python -m mcpfromzero.serve`."""
from .server import Server


def build() -> Server:
    s = Server()
    s.instructions(
        "MCP from Zero reference server. Built against protocol revision "
        "2026-07-28, which is stateless - no initialize handshake."
    )
    return s


if __name__ == "__main__":
    build().serve_stdio()

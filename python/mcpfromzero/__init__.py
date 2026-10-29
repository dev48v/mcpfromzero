"""MCP from Zero - a nine-level teardown of the Model Context Protocol.

Built against protocol revision 2026-07-28, which is stateless: there is no
`initialize` handshake, no `notifications/initialized`, and no session.
"""
from .jsonrpc import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, RpcError
from .server import Server

__all__ = ["Server", "RpcError", "LATEST_PROTOCOL_VERSION", "SUPPORTED_PROTOCOL_VERSIONS"]
__version__ = "0.1.0"

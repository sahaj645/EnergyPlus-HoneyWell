"""MCP server entry point.

The ``mcp`` SDK will be imported inside :func:`build_server`, not at module level, so that
importing this module - which the tests do - never opens a transport or requires the SDK.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from mcp_server.tools import TOOL_NAMES

SERVER_NAME = "hive"


def build_server() -> object:
    """Construct the MCP server and register :data:`~mcp_server.tools.TOOL_NAMES`."""
    raise NotImplementedError("MCP server construction not implemented yet (scaffold)")


def serve() -> None:
    """Run the server on stdio. Blocking; call from ``__main__`` only."""
    raise NotImplementedError("MCP serve loop not implemented yet (scaffold)")


def main() -> None:
    """CLI entry point: ``python -m mcp_server.server``."""
    print(f"{SERVER_NAME}: {len(TOOL_NAMES)} tools registered -> {', '.join(TOOL_NAMES)}")
    serve()


if __name__ == "__main__":
    main()

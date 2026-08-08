"""Entry point for the Jeeves MCP server: python3 -m jeeves.mcp.server"""

from __future__ import annotations

import sys

from . import protocol, tools  # noqa: F401  (importing tools performs registration)


def main() -> int:
    return protocol.serve()


if __name__ == "__main__":
    sys.exit(main())

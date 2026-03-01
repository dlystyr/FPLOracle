"""FPLOracle MCP server entry point."""

from fpl_oracle.server import mcp
from fpl_oracle.config import settings

if __name__ == "__main__":
    mcp.run(transport="sse", host=settings.host, port=settings.port)

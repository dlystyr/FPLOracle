"""FastMCP server setup and lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from fpl_oracle.config import settings
from fpl_oracle.log import setup_logging, get_logger

setup_logging()
log = get_logger(__name__)

_sync_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    from fpl_oracle import db, cache
    from fpl_oracle.fpl_api import close_client

    global _sync_task

    log.info("server_starting")
    await db.init_db()
    await cache.init_cache()

    # Start background sync loop
    _sync_task = asyncio.create_task(_sync_loop())
    log.info("server_started")

    yield

    log.info("server_stopping")
    if _sync_task:
        _sync_task.cancel()
    await close_client()
    await cache.close_cache()
    await db.close_db()
    log.info("server_stopped")


mcp = FastMCP(
    name="FPLOracle",
    version="1.0.0",
    instructions=(
        "FPL analytics server. Use search_players to find player IDs, "
        "then use other tools with those IDs. Use my_team with a manager_id "
        "for personalized analysis."
    ),
    lifespan=lifespan,
)

# Register all tools (importing triggers @mcp.tool decorators)
from fpl_oracle.tools import players  # noqa: E402, F401
from fpl_oracle.tools import rankings  # noqa: E402, F401
from fpl_oracle.tools import manager  # noqa: E402, F401
from fpl_oracle.tools import fixtures_tools  # noqa: E402, F401
from fpl_oracle.tools import captaincy  # noqa: E402, F401
from fpl_oracle.tools import live  # noqa: E402, F401
from fpl_oracle.tools import regression  # noqa: E402, F401
from fpl_oracle.tools import team_tools  # noqa: E402, F401
from fpl_oracle.tools import planning  # noqa: E402, F401
from fpl_oracle.tools import rotation  # noqa: E402, F401
from fpl_oracle.tools import opponent  # noqa: E402, F401
from fpl_oracle.tools import ownership_tools  # noqa: E402, F401
from fpl_oracle.tools import set_pieces  # noqa: E402, F401
from fpl_oracle.tools import defensive  # noqa: E402, F401
from fpl_oracle.tools import xcs_tool  # noqa: E402, F401


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "healthy", "server": "FPLOracle", "version": "1.0.0"})


async def _sync_loop():
    """Background sync that runs on startup then every SYNC_INTERVAL seconds."""
    from fpl_oracle.sync import run_sync

    # Initial sync after short delay to let everything initialize
    await asyncio.sleep(2)
    try:
        result = await run_sync()
        log.info("initial_sync_result", **result)
    except Exception:
        log.error("initial_sync_failed", exc_info=True)

    # Recurring sync
    while True:
        await asyncio.sleep(settings.sync_interval)
        try:
            result = await run_sync()
            log.info("scheduled_sync_result", **result)
        except Exception:
            log.error("scheduled_sync_failed", exc_info=True)

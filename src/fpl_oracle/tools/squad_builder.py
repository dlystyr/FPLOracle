"""Squad builder tool: optimal squad via LP solver."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle.analytics.optimizer import build_squad
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def build_squad_tool(
    budget: Annotated[float, "Total budget in m (default 100)"] = 100.0,
    strategy: Annotated[str, "'balanced', 'attacking' (boost FWD/MID), or 'defensive' (boost GK/DEF)"] = "balanced",
    must_include: Annotated[list[int] | None, "Player IDs that must be in the squad"] = None,
    exclude: Annotated[list[int] | None, "Player IDs to exclude"] = None,
    gameweek: Annotated[int | None, "Optimize for a single GW (free hit mode)"] = None,
) -> dict:
    """Build optimal 15-player squad using linear programming. Maximizes xPts subject to budget (100m), position limits (2GK/5DEF/5MID/3FWD), and max 3 per team. Set gameweek for free hit optimization."""
    return await build_squad(
        budget=budget,
        strategy=strategy,
        must_include=must_include,
        exclude=exclude,
        gameweek=gameweek,
    )

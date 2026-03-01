"""Fixture tools: team outlook, DGW/BGW detection, deadline info."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.analytics import fixtures as fix_analytics
from fpl_oracle.models import TeamOutlook, FixtureInfo
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def fixture_outlook(
    team: Annotated[str | None, "Team short name e.g. ARS, or omit for all teams"] = None,
    num_fixtures: Annotated[int, "Number of upcoming fixtures (default 5)"] = 5,
    include_dgw_bgw: Annotated[bool, "Include DGW/BGW detection (default true)"] = True,
) -> dict:
    """Fixture difficulty outlook with DGW/BGW detection. Omit team for league-wide ranking."""
    result: dict = {}

    if team:
        # Single team
        team_row = await db.fetch_one(
            "SELECT id FROM teams WHERE UPPER(short_name) = $1", team.upper()
        )
        if not team_row:
            return {"error": f"Team '{team}' not found"}

        outlook = await fix_analytics.team_fixture_outlook(
            team_row["id"], num_fixtures
        )
        result["team_outlook"] = outlook
    else:
        # All teams ranked
        outlooks = await fix_analytics.all_teams_outlook(num_fixtures)
        result["rankings"] = [
            {"team": o["team"], "avg_diff": o["avg_difficulty"], "quality": o["run_quality"]}
            for o in outlooks
        ]

    if include_dgw_bgw:
        dgw_bgw = await fix_analytics.detect_dgw_bgw()
        result["double_gameweeks"] = dgw_bgw["double_gameweeks"]
        result["blank_gameweeks"] = dgw_bgw["blank_gameweeks"]

    return result

"""Clean sheet probability tool."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.analytics.clean_sheets import team_xcs
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def clean_sheet_odds(
    team: Annotated[str | None, "Team short name e.g. ARS, or omit for league ranking"] = None,
    num_fixtures: Annotated[int, "Number of upcoming fixtures (default 5)"] = 5,
) -> dict | list[dict]:
    """Expected clean sheet probability per fixture using Poisson model. Helps evaluate defensive assets (GK/DEF). Higher xCS = better for clean sheet points."""
    if team:
        team_row = await db.fetch_one(
            "SELECT id, short_name FROM teams WHERE UPPER(short_name) = $1",
            team.upper(),
        )
        if not team_row:
            return {"error": f"Team '{team}' not found"}

        xcs_data = await team_xcs(team_row["id"], num_fixtures)
        avg_xcs = sum(x["xcs"] for x in xcs_data) / len(xcs_data) if xcs_data else 0

        return {
            "team": team_row["short_name"],
            "avg_xcs": round(avg_xcs, 3),
            "avg_xcs_pct": f"{avg_xcs * 100:.1f}%",
            "fixtures": xcs_data,
            "verdict": (
                "Excellent for defensive assets" if avg_xcs >= 0.35 else
                "Good CS potential" if avg_xcs >= 0.25 else
                "Average CS potential" if avg_xcs >= 0.18 else
                "Poor for defensive assets"
            ),
        }

    # League-wide ranking
    teams = await db.fetch_all("SELECT id, short_name FROM teams ORDER BY id")
    rankings = []
    for t in teams:
        xcs_data = await team_xcs(t["id"], num_fixtures)
        avg_xcs = sum(x["xcs"] for x in xcs_data) / len(xcs_data) if xcs_data else 0
        rankings.append({
            "team": t["short_name"],
            "avg_xcs": round(avg_xcs, 3),
            "avg_xcs_pct": f"{avg_xcs * 100:.1f}%",
            "next_xcs": xcs_data[0] if xcs_data else None,
        })

    rankings.sort(key=lambda x: x["avg_xcs"], reverse=True)
    return rankings

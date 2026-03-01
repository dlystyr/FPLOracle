"""Set piece takers tool: corners, free kicks, penalties by team."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.models import POS_MAP
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def set_piece_takers(
    team: Annotated[str | None, "Team short name e.g. ARS, or omit for all teams"] = None,
) -> dict | list[dict]:
    """Identify set piece takers by team. 35%+ of PL goals come from set pieces — corners, free kicks, and penalties are a hidden xA/xG multiplier. Uses creativity + threat + penalty data as proxies."""
    if team:
        team_row = await db.fetch_one(
            "SELECT id, short_name, name FROM teams WHERE UPPER(short_name) = $1",
            team.upper(),
        )
        if not team_row:
            return {"error": f"Team '{team}' not found"}

        return await _team_set_pieces(team_row["id"], team_row["short_name"])

    # All teams
    teams = await db.fetch_all("SELECT id, short_name FROM teams ORDER BY short_name")
    results = []
    for t in teams:
        sp = await _team_set_pieces(t["id"], t["short_name"])
        results.append(sp)
    return results


async def _team_set_pieces(team_id: int, team_name: str) -> dict:
    """Analyse set piece duties for a team."""
    # Penalty takers: players with penalty involvement
    pen_takers = await db.fetch_all(
        "SELECT p.id, p.web_name, p.element_type, p.now_cost, "
        "p.penalties_saved, p.penalties_missed, "
        "(SELECT SUM(ph.goals_scored) FROM player_history ph "
        " WHERE ph.player_id = p.id) AS total_goals "
        "FROM players p WHERE p.team_id = $1 "
        "AND (p.penalties_saved > 0 OR p.penalties_missed > 0 OR "
        "     p.goals_scored > 0) "
        "AND p.status = 'a' "
        "ORDER BY (p.penalties_saved + p.penalties_missed) DESC, p.goals_scored DESC "
        "LIMIT 3",
        team_id,
    )

    # Corner/FK takers: highest creativity + assists among outfield
    creative = await db.fetch_all(
        "SELECT p.id, p.web_name, p.element_type, p.now_cost, "
        "p.creativity, p.threat, p.assists, p.expected_assists, "
        "p.form "
        "FROM players p WHERE p.team_id = $1 "
        "AND p.element_type IN (2, 3, 4) "
        "AND p.status = 'a' AND p.minutes >= 270 "
        "ORDER BY p.creativity DESC LIMIT 5",
        team_id,
    )

    # Direct FK / threat leaders (likely free kick takers)
    threat_leaders = await db.fetch_all(
        "SELECT p.id, p.web_name, p.element_type, p.now_cost, "
        "p.threat, p.goals_scored, p.expected_goals "
        "FROM players p WHERE p.team_id = $1 "
        "AND p.element_type IN (2, 3, 4) "
        "AND p.status = 'a' AND p.minutes >= 270 "
        "ORDER BY p.threat DESC LIMIT 3",
        team_id,
    )

    def _player_info(r: dict) -> dict:
        return {
            "id": r["id"],
            "name": r["web_name"],
            "pos": POS_MAP.get(r["element_type"], "???"),
            "price": round(r["now_cost"] / 10, 1),
        }

    penalties = []
    for p in pen_takers:
        pen_count = (p.get("penalties_saved", 0) or 0) + (p.get("penalties_missed", 0) or 0)
        if pen_count > 0 or p.get("total_goals", 0):
            penalties.append({
                **_player_info(p),
                "penalties_taken": pen_count,
                "role": "primary" if pen_count > 0 else "possible",
            })

    corners = []
    for i, c in enumerate(creative[:3]):
        corners.append({
            **_player_info(c),
            "creativity": round(float(c["creativity"]), 1),
            "assists": c.get("assists", 0),
            "xa": round(float(c.get("expected_assists", 0) or 0), 2),
            "role": "primary" if i == 0 else "secondary",
        })

    free_kicks = []
    for i, t in enumerate(threat_leaders[:2]):
        free_kicks.append({
            **_player_info(t),
            "threat": round(float(t["threat"]), 1),
            "goals": t.get("goals_scored", 0),
            "xg": round(float(t.get("expected_goals", 0) or 0), 2),
            "role": "primary" if i == 0 else "secondary",
        })

    return {
        "team": team_name,
        "penalties": penalties,
        "corners": corners,
        "free_kicks": free_kicks,
        "set_piece_value": (
            "Set pieces account for 35%+ of PL goals. "
            "Players on corner, FK, and penalty duty get a hidden xA/xG multiplier."
        ),
    }

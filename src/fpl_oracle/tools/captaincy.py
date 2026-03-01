"""Captaincy tool: pick recommendations for a manager's squad."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db, fpl_api
from fpl_oracle.models import POS_MAP, CaptainPick, PlayerRef
from fpl_oracle.analytics import xpts, scoring
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def captain_picks(
    manager_id: Annotated[int, "FPL manager ID"],
    limit: Annotated[int, "Number of picks to return (default 3)"] = 3,
) -> list[dict]:
    """Captain recommendations for your squad based on xPts, form, fixtures, and opponent history."""
    current_gw_row = await db.fetch_one(
        "SELECT id FROM events WHERE is_current = TRUE LIMIT 1"
    )
    current_gw = current_gw_row["id"] if current_gw_row else 1

    try:
        picks_data = await fpl_api.manager_picks(manager_id, current_gw)
    except Exception:
        return [{"error": "Could not fetch picks"}]

    if not picks_data or "picks" not in picks_data:
        return [{"error": "No picks data"}]

    squad_ids = [p["element"] for p in picks_data["picks"] if p.get("multiplier", 0) > 0]
    team_strengths = await xpts.calculate_team_strength()

    candidates: list[dict] = []

    for pid in squad_ids:
        row = await db.fetch_one(
            "SELECT p.*, t.short_name FROM players p "
            "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
            pid,
        )
        if not row:
            continue

        fixtures = await db.fetch_all(
            "SELECT f.*, t2.short_name AS opp_name FROM fixtures f "
            "JOIN teams t2 ON t2.id = CASE WHEN f.team_h = $1 THEN f.team_a ELSE f.team_h END "
            "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
            "ORDER BY f.event LIMIT 1",
            row["team_id"],
        )

        if not fixtures:
            continue

        next_fix = fixtures[0]
        is_home = next_fix["team_h"] == row["team_id"]
        diff = next_fix.get("team_a_difficulty", 3) if is_home else next_fix.get("team_h_difficulty", 3)

        # xP for next GW
        score_data = await scoring.score_player(
            row, fixtures, team_strengths, current_gw
        )
        gw_xp = score_data["per_gw"][0]["xpts"] if score_data.get("per_gw") else score_data["xpts"] / 5

        player_form = float(row.get("form", 0) or 0)

        # Opponent history bonus
        opp_id = next_fix["team_a"] if is_home else next_fix["team_h"]
        opp_hist = await db.fetch_one(
            "SELECT avg_points FROM player_opponent_history "
            "WHERE player_id = $1 AND opponent_id = $2",
            pid, opp_id,
        )
        vs_opp = float(opp_hist["avg_points"]) if opp_hist else 0

        captain_score = (
            gw_xp * 15
            + (6 - diff) * 3
            + player_form * 2
            + vs_opp * 2
            + (2 if is_home else 0)
        )

        opp_label = f"{next_fix['opp_name']} ({'H' if is_home else 'A'})"

        candidates.append(CaptainPick(
            player=PlayerRef(
                id=row["id"],
                name=row["web_name"],
                team=row["short_name"],
                pos=POS_MAP.get(row["element_type"], "???"),
                price=round(row["now_cost"] / 10, 1),
            ),
            captain_score=round(captain_score, 2),
            next_fixture=opp_label,
            xpts=round(gw_xp, 2),
            form=player_form,
        ).model_dump())

    candidates.sort(key=lambda x: x["captain_score"], reverse=True)
    return candidates[:limit]

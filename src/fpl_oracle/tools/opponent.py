"""Opponent history tool: bogey teams and favourite opponents."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.models import POS_MAP
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def bogey_teams(
    player_id: Annotated[int, "Player ID to check"],
    opponent: Annotated[str | None, "Specific opponent short name (e.g. MCI), or omit for all"] = None,
) -> dict:
    """Historical player performance vs specific opponents. Shows bogey teams (underperform) and favourite opponents (overperform). Useful for timing transfers around fixtures."""
    # Get player info
    player = await db.fetch_one(
        "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost, "
        "p.points_per_game "
        "FROM players p JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
        player_id,
    )
    if not player:
        return {"error": "Player not found"}

    avg_ppg = float(player.get("points_per_game", 0) or 0)

    if opponent:
        # Specific opponent
        opp_team = await db.fetch_one(
            "SELECT id, short_name, name FROM teams WHERE UPPER(short_name) = $1",
            opponent.upper(),
        )
        if not opp_team:
            return {"error": f"Team '{opponent}' not found"}

        history = await db.fetch_all(
            "SELECT ph.event, ph.total_points, ph.goals_scored, ph.assists, "
            "ph.was_home, ph.minutes "
            "FROM player_history ph WHERE ph.player_id = $1 AND ph.opponent_team = $2 "
            "ORDER BY ph.event DESC",
            player_id, opp_team["id"],
        )

        if not history:
            # Check pre-computed data
            pre = await db.fetch_one(
                "SELECT * FROM player_opponent_history "
                "WHERE player_id = $1 AND opponent_id = $2",
                player_id, opp_team["id"],
            )
            if pre:
                return {
                    "player": player["web_name"],
                    "opponent": opp_team["short_name"],
                    "games": pre["games_played"],
                    "avg_points": float(pre["avg_points"]),
                    "total_goals": pre["goals"],
                    "total_assists": pre["assists"],
                    "is_bogey": pre["is_bogey_team"],
                    "is_favourite": pre["is_favourite"],
                }
            return {"player": player["web_name"], "opponent": opponent, "games": 0}

        total_pts = sum(h["total_points"] or 0 for h in history)
        avg_pts = total_pts / len(history)
        total_g = sum(h["goals_scored"] or 0 for h in history)
        total_a = sum(h["assists"] or 0 for h in history)

        is_bogey = avg_pts < avg_ppg * 0.7 if avg_ppg > 0 else False
        is_fav = avg_pts > avg_ppg * 1.3 if avg_ppg > 0 else False

        return {
            "player": player["web_name"],
            "opponent": opp_team["short_name"],
            "games": len(history),
            "avg_points": round(avg_pts, 2),
            "player_avg_ppg": avg_ppg,
            "total_goals": total_g,
            "total_assists": total_a,
            "is_bogey": is_bogey,
            "is_favourite": is_fav,
            "verdict": "bogey team" if is_bogey else ("favourite opponent" if is_fav else "neutral"),
            "matches": [
                {
                    "gw": h["event"],
                    "pts": h["total_points"],
                    "g": h["goals_scored"],
                    "a": h["assists"],
                    "home": h["was_home"],
                }
                for h in history[:10]
            ],
        }

    # All opponents summary
    opponents = await db.fetch_all(
        "SELECT poh.*, t.short_name AS opponent_name "
        "FROM player_opponent_history poh "
        "JOIN teams t ON poh.opponent_id = t.id "
        "WHERE poh.player_id = $1 AND poh.games_played >= 2 "
        "ORDER BY poh.avg_points ASC",
        player_id,
    )

    bogeys = [
        {
            "opponent": o["opponent_name"],
            "games": o["games_played"],
            "avg_pts": float(o["avg_points"]),
            "goals": o["goals"],
            "assists": o["assists"],
        }
        for o in opponents if o["is_bogey_team"]
    ]

    favourites = [
        {
            "opponent": o["opponent_name"],
            "games": o["games_played"],
            "avg_pts": float(o["avg_points"]),
            "goals": o["goals"],
            "assists": o["assists"],
        }
        for o in opponents if o["is_favourite"]
    ]

    return {
        "player": player["web_name"],
        "player_avg_ppg": avg_ppg,
        "bogey_teams": bogeys,
        "favourite_opponents": favourites,
        "total_opponents_analyzed": len(opponents),
    }

"""Rotation risk tool: minutes prediction and nailed score."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.analytics.form import nailed_score
from fpl_oracle.models import POS_MAP
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def rotation_risk(
    player_id: Annotated[int | None, "Player ID to check"] = None,
    name: Annotated[str | None, "Player name to check"] = None,
    show_risky: Annotated[bool, "Show top rotation risks across the league (default false)"] = False,
    limit: Annotated[int, "Max results for league-wide view (default 15)"] = 15,
) -> dict | list[dict]:
    """Minutes prediction and rotation risk. Returns nailed score (0-100), start rate, recent minutes pattern, and risk classification."""
    if player_id or name:
        if player_id:
            row = await db.fetch_one(
                "SELECT p.*, t.short_name FROM players p "
                "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
                player_id,
            )
        else:
            row = await db.fetch_one(
                "SELECT p.*, t.short_name FROM players p "
                "JOIN teams t ON p.team_id = t.id "
                "WHERE LOWER(p.web_name) = $1 LIMIT 1",
                name.lower(),
            )
        if not row:
            return {"error": "Player not found"}

        return await _analyze_rotation(row)

    if show_risky:
        # Find players with high ownership but rotation risk
        rows = await db.fetch_all(
            "SELECT p.*, t.short_name FROM players p "
            "JOIN teams t ON p.team_id = t.id "
            "WHERE p.selected_by_percent >= 5 AND p.minutes >= 180 "
            "AND p.status = 'a' "
            "ORDER BY p.selected_by_percent DESC LIMIT $1",
            limit * 2,
        )
        results = []
        for r in rows:
            analysis = await _analyze_rotation(r)
            if analysis.get("risk") in ("high", "medium"):
                results.append(analysis)
        results.sort(key=lambda x: x.get("nailed_score", 100))
        return results[:limit]

    return {"error": "Provide player_id, name, or set show_risky=true"}


async def _analyze_rotation(row: dict) -> dict:
    """Full rotation analysis for a player."""
    player_id = row["id"]

    # Recent history
    history = await db.fetch_all(
        "SELECT minutes, total_points, event FROM player_history "
        "WHERE player_id = $1 ORDER BY event DESC LIMIT 8",
        player_id,
    )
    history_dicts = [dict(h) for h in history]

    score = nailed_score(row, history_dicts)

    # Detailed minutes pattern
    recent_minutes = [h.get("minutes", 0) or 0 for h in history_dicts[:6]]
    starts_recent = sum(1 for m in recent_minutes if m >= 60)
    sub_appearances = sum(1 for m in recent_minutes if 0 < m < 60)
    benched = sum(1 for m in recent_minutes if m == 0)

    total_starts = row.get("starts", 0) or 0
    total_minutes = row.get("minutes", 0) or 0
    avg_mins = total_minutes / max(total_starts + sub_appearances, 1)

    # Classify
    if score >= 85:
        risk = "low"
        summary = "Nailed starter, very safe"
    elif score >= 70:
        risk = "low-medium"
        summary = "Regular starter, occasional rest"
    elif score >= 55:
        risk = "medium"
        summary = "Rotation candidate, monitor weekly"
    elif score >= 35:
        risk = "high"
        summary = "Heavy rotation risk, unreliable starter"
    else:
        risk = "very_high"
        summary = "Bench player or injured, avoid"

    result = {
        "id": row["id"],
        "name": row["web_name"],
        "team": row["short_name"],
        "pos": POS_MAP.get(row["element_type"], "???"),
        "price": round(row["now_cost"] / 10, 1),
        "nailed_score": score,
        "risk": risk,
        "summary": summary,
        "avg_minutes": round(avg_mins, 1),
        "recent_6_games": {
            "starts": starts_recent,
            "sub_appearances": sub_appearances,
            "benched": benched,
            "minutes": recent_minutes,
        },
        "season": {
            "total_starts": total_starts,
            "total_minutes": total_minutes,
        },
        "status": row.get("status", "a"),
        "chance_next": row.get("chance_of_playing_next_round"),
        "news": row.get("news") or None,
    }

    if row.get("selected_by_percent"):
        result["ownership"] = float(row["selected_by_percent"])

    return result

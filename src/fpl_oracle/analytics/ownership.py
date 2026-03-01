"""Ownership analytics: effective ownership, template players, differential scoring."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.log import get_logger

log = get_logger(__name__)


async def template_players(
    min_ownership: float = 20.0,
    position: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Highly-owned players that form the 'template' team.

    These are must-have players: missing a haul from a template player
    damages your rank because most managers own them.
    """
    conditions = ["p.selected_by_percent >= $1", "p.status = 'a'"]
    params: list = [min_ownership]
    idx = 2

    if position:
        conditions.append(f"p.element_type = ${idx}")
        params.append(position)
        idx += 1

    sql = (
        f"SELECT p.id, p.web_name, t.short_name, p.element_type, "
        f"p.now_cost, p.selected_by_percent, p.form, p.total_points, "
        f"p.points_per_game "
        f"FROM players p JOIN teams t ON p.team_id = t.id "
        f"WHERE {' AND '.join(conditions)} "
        f"ORDER BY p.selected_by_percent DESC LIMIT ${idx}"
    )
    params.append(limit)

    rows = await db.fetch_all(sql, *params)
    return [
        {
            "id": r["id"],
            "name": r["web_name"],
            "team": r["short_name"],
            "ownership": float(r["selected_by_percent"]),
            "form": float(r.get("form", 0) or 0),
            "points": r.get("total_points", 0),
            "ppg": float(r.get("points_per_game", 0) or 0),
            "price": round(r["now_cost"] / 10, 1),
            "template_risk": "high" if float(r["selected_by_percent"]) >= 40 else "medium",
        }
        for r in rows
    ]


async def effective_ownership(player_id: int) -> dict[str, Any]:
    """Calculate effective ownership for a player.

    EO = ownership% + estimated captaincy% (based on most_captained data).
    True EO requires live gameweek data; this is an estimate.
    """
    player = await db.fetch_one(
        "SELECT p.id, p.web_name, p.selected_by_percent, t.short_name "
        "FROM players p JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
        player_id,
    )
    if not player:
        return {"error": "Player not found"}

    ownership = float(player["selected_by_percent"])

    # Check if most captained in current/recent GW
    event = await db.fetch_one(
        "SELECT most_captained, most_vice_captained FROM events "
        "WHERE is_current = TRUE OR is_previous = TRUE ORDER BY id DESC LIMIT 1"
    )

    captain_boost = 0.0
    if event and event["most_captained"] == player_id:
        captain_boost = ownership * 0.3  # ~30% of owners also captain
    elif event and event["most_vice_captained"] == player_id:
        captain_boost = ownership * 0.05

    eo = ownership + captain_boost

    return {
        "id": player_id,
        "name": player["web_name"],
        "team": player["short_name"],
        "ownership": ownership,
        "estimated_captaincy": round(captain_boost, 2),
        "effective_ownership": round(eo, 2),
        "differential_threshold": eo < 15,
        "must_have_threshold": eo > 35,
    }


async def differential_impact(
    player_id: int, expected_points: float
) -> dict[str, Any]:
    """Calculate rank impact of owning a differential player.

    Low EO + high xP = maximum rank gain potential.
    """
    eo_data = await effective_ownership(player_id)
    if "error" in eo_data:
        return eo_data

    eo = eo_data["effective_ownership"]
    # Differential score: how much rank you gain per point this player scores
    # Higher when fewer people own the player
    differential_score = expected_points * (100 - eo) / 100

    return {
        **eo_data,
        "expected_points": expected_points,
        "differential_score": round(differential_score, 2),
        "rank_impact": (
            "high" if differential_score > 8 else
            "medium" if differential_score > 4 else
            "low"
        ),
    }


def vapm(total_points: int, minutes: int, price: float, element_type: int) -> float:
    """Value Added Per Million (VAPM).

    Strips appearance points and normalizes by price above positional minimum.
    VAPM = (total_points - appearance_points) / adjusted_price
    """
    if minutes == 0 or price <= 0:
        return 0.0

    # Positional minimum prices (typical)
    min_prices = {1: 4.0, 2: 4.0, 3: 4.5, 4: 4.5}
    min_price = min_prices.get(element_type, 4.0)
    adjusted_price = max(price - min_price, 0.5)

    # Estimate appearance points (2 per appearance with >60 mins)
    appearances = minutes / 90  # rough estimate
    appearance_pts = appearances * 2

    value_points = max(total_points - appearance_pts, 0)
    return round(value_points / adjusted_price, 2)

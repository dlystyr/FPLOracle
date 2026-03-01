"""Hit evaluation: is a -4 transfer worth the cost?"""

from __future__ import annotations

from typing import Any

from fpl_oracle.analytics import xpts, scoring
from fpl_oracle import db
from fpl_oracle.log import get_logger

log = get_logger(__name__)

HIT_COST = 4


async def evaluate_hit(
    player_out_id: int,
    player_in_id: int,
    horizon: int = 5,
) -> dict[str, Any]:
    """Evaluate whether a -4 hit is worth it.

    Calculates xPts differential over the given horizon minus the 4-point cost.
    """
    team_strengths = await xpts.calculate_team_strength()
    current_gw_row = await db.fetch_one(
        "SELECT id FROM events WHERE is_current = TRUE LIMIT 1"
    )
    current_gw = current_gw_row["id"] if current_gw_row else 1

    # Score player out
    p_out = await db.fetch_one(
        "SELECT p.*, t.short_name FROM players p "
        "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
        player_out_id,
    )
    if not p_out:
        return {"error": f"Player {player_out_id} not found"}

    fix_out = await db.fetch_all(
        "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
        "AND NOT finished ORDER BY event LIMIT $2",
        p_out["team_id"], horizon,
    )
    xp_out_data = await xpts.calculate_expected_points(
        p_out, fix_out, team_strengths, current_gw
    )

    # Score player in
    p_in = await db.fetch_one(
        "SELECT p.*, t.short_name FROM players p "
        "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
        player_in_id,
    )
    if not p_in:
        return {"error": f"Player {player_in_id} not found"}

    fix_in = await db.fetch_all(
        "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
        "AND NOT finished ORDER BY event LIMIT $2",
        p_in["team_id"], horizon,
    )
    xp_in_data = await xpts.calculate_expected_points(
        p_in, fix_in, team_strengths, current_gw
    )

    xp_out = xp_out_data["total"]
    xp_in = xp_in_data["total"]
    xp_differential = xp_in - xp_out
    net_gain = xp_differential - HIT_COST

    # Per-GW rates
    xp_out_per_gw = xp_out / max(horizon, 1)
    xp_in_per_gw = xp_in / max(horizon, 1)
    gw_diff = xp_in_per_gw - xp_out_per_gw

    # Breakeven weeks
    if gw_diff > 0:
        breakeven = HIT_COST / gw_diff
    else:
        breakeven = float("inf")

    # Decision logic
    if net_gain > 5:
        verdict = "take_hit"
        confidence = "high"
    elif net_gain > 2:
        verdict = "take_hit"
        confidence = "medium"
    elif net_gain > 0:
        verdict = "wait"
        confidence = "low"
    else:
        verdict = "avoid"
        confidence = "high"

    # Override: player out is injured/suspended
    if p_out.get("status") != "a" and net_gain > -2:
        verdict = "take_hit"
        confidence = "medium"
        override = "player_out unavailable"
    # Override: player in is also flagged
    elif p_in.get("status") != "a":
        verdict = "avoid"
        confidence = "high"
        override = "player_in also flagged"
    else:
        override = None

    result: dict[str, Any] = {
        "player_out": {"id": p_out["id"], "name": p_out["web_name"], "team": p_out["short_name"]},
        "player_in": {"id": p_in["id"], "name": p_in["web_name"], "team": p_in["short_name"]},
        "horizon_gws": horizon,
        "xpts_out": round(xp_out, 2),
        "xpts_in": round(xp_in, 2),
        "xpts_differential": round(xp_differential, 2),
        "hit_cost": HIT_COST,
        "net_gain": round(net_gain, 2),
        "breakeven_weeks": round(breakeven, 1) if breakeven != float("inf") else "never",
        "verdict": verdict,
        "confidence": confidence,
    }
    if override:
        result["override_reason"] = override

    return result

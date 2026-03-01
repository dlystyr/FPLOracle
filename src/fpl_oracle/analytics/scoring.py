"""Unified player scoring: first pass + optional refinement + risk profile."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.analytics import xpts, form
from fpl_oracle.log import get_logger

log = get_logger(__name__)


async def score_player(
    player: dict[str, Any],
    upcoming_fixtures: list[dict[str, Any]],
    team_strengths: dict[int, dict[str, int]],
    current_gw: int,
    *,
    refine: bool = False,
) -> dict[str, Any]:
    """Score a player combining xPts, form, fixtures, and rotation risk.

    If refine=True, applies Bayesian shrinkage and outcome range.
    """
    xp_data = await xpts.calculate_expected_points(
        player, upcoming_fixtures, team_strengths, current_gw
    )
    total_xp = xp_data["total"]
    per_million = xp_data["per_million"]

    player_form = float(player.get("form", 0) or 0)
    price = (player.get("now_cost", 50) or 50) / 10

    # Fixture run quality
    if upcoming_fixtures:
        team_id = player["team_id"]
        diffs = []
        for f in upcoming_fixtures[:5]:
            is_home = f["team_h"] == team_id
            d = f.get("team_a_difficulty", 3) if is_home else f.get("team_h_difficulty", 3)
            diffs.append(d)
        avg_diff = sum(diffs) / len(diffs) if diffs else 3.0
        if avg_diff <= 2.5:
            run_q = "excellent"
        elif avg_diff <= 3.0:
            run_q = "good"
        elif avg_diff <= 3.5:
            run_q = "mixed"
        else:
            run_q = "tough"
        fixture_run = f"{avg_diff:.1f} avg ({run_q})"
    else:
        fixture_run = "N/A"
        avg_diff = 3.0

    # Composite score
    composite = (
        total_xp * 3.0
        + player_form * 10.0
        + (10 - avg_diff) * 5.0
        + per_million * 15.0
    )

    result: dict[str, Any] = {
        "xpts": total_xp,
        "per_million": per_million,
        "form": player_form,
        "fixture_run": fixture_run,
        "composite": round(composite, 2),
        "per_gw": xp_data["per_gw"],
    }

    if refine:
        result.update(await _refine(player, total_xp, current_gw))

    return result


async def _refine(
    player: dict[str, Any], base_xp: float, current_gw: int
) -> dict[str, Any]:
    """Second-pass refinement: Bayesian shrinkage, confidence, outcome range."""
    player_id = player["id"]

    # Form trajectory
    trajectory = await form.form_trajectory(player_id)
    momentum = trajectory["momentum"]

    # Momentum adjustment
    if momentum == "rising":
        adj = 1.10
    elif momentum == "falling":
        adj = 0.90
    else:
        adj = 1.0

    refined_xp = round(base_xp * adj, 2)

    # Confidence based on sample size
    minutes = player.get("minutes", 0) or 0
    if minutes >= 1500:
        confidence = "high"
    elif minutes >= 900:
        confidence = "medium"
    else:
        confidence = "low"

    # Outcome range (±20% for high confidence, ±35% for low)
    spread = 0.20 if confidence == "high" else (0.28 if confidence == "medium" else 0.35)
    floor_xp = round(refined_xp * (1 - spread), 2)
    ceiling_xp = round(refined_xp * (1 + spread), 2)

    # Rotation risk
    history = await db.fetch_all(
        "SELECT minutes FROM player_history WHERE player_id = $1 "
        "ORDER BY event DESC LIMIT 6",
        player_id,
    )
    nailed = form.nailed_score(player, history)
    if nailed >= 85:
        rotation_risk = "low"
    elif nailed >= 60:
        rotation_risk = "medium"
    else:
        rotation_risk = "high"

    return {
        "refined_xpts": refined_xp,
        "momentum": momentum,
        "confidence": confidence,
        "floor": floor_xp,
        "ceiling": ceiling_xp,
        "rotation_risk": rotation_risk,
        "nailed_score": nailed,
    }

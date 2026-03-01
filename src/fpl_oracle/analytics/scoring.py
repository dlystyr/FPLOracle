"""Unified player scoring: first pass + optional refinement + risk profile.

v2: Context-aware scoring that factors in opponent quality, bogey teams,
opponent form, venue splits, and rolling xGI form — not just raw FDR numbers.
"""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.analytics import xpts, form
from fpl_oracle.analytics.venue import venue_splits
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
    """Score a player combining xPts, form, fixtures, and contextual factors.

    Context-aware improvements:
    - Opponent actual form (not just FDR) penalizes/boosts
    - Bogey team warnings surface in the score
    - Venue splits adjust for home/away specialists
    - Rolling xGI form replaces raw FPL form in weighting
    """
    player_id = player["id"]
    team_id = player["team_id"]

    xp_data = await xpts.calculate_expected_points(
        player, upcoming_fixtures, team_strengths, current_gw
    )
    total_xp = xp_data["total"]
    per_million = xp_data["per_million"]

    player_form = float(player.get("form", 0) or 0)
    price = (player.get("now_cost", 50) or 50) / 10

    # --- Fixture run quality (from opponent actual conceding rates) ---
    context_warnings: list[str] = []
    opp_quality_scores: list[float] = []

    for f in upcoming_fixtures[:5]:
        is_home = f["team_h"] == team_id
        opp_id = f["team_a"] if is_home else f["team_h"]

        # Opponent's recent form
        opp_results = await db.fetch_one(
            "SELECT AVG(goals_for) AS avg_gf, AVG(goals_against) AS avg_ga, "
            "COUNT(*) FILTER (WHERE result = 'W') AS wins, COUNT(*) AS games "
            "FROM (SELECT * FROM team_results WHERE team_id = $1 ORDER BY event DESC LIMIT 5) sub",
            opp_id,
        )

        if opp_results and opp_results["games"]:
            opp_win_rate = opp_results["wins"] / opp_results["games"]
            opp_ga = float(opp_results["avg_ga"] or 1.3)
            # 0 = hardest (opponent wins everything and concedes nothing)
            # 10 = easiest (opponent loses and concedes a lot)
            quality = (1 - opp_win_rate) * 5 + min(opp_ga, 2) * 2.5
        else:
            quality = 5.0  # neutral

        opp_quality_scores.append(quality)

        # Check for bogey teams
        bogey = await db.fetch_one(
            "SELECT is_bogey_team, is_favourite, avg_points "
            "FROM player_opponent_history "
            "WHERE player_id = $1 AND opponent_id = $2",
            player_id, opp_id,
        )
        if bogey and bogey["is_bogey_team"]:
            opp_name = await db.fetch_one(
                "SELECT short_name FROM teams WHERE id = $1", opp_id
            )
            name = opp_name["short_name"] if opp_name else "???"
            context_warnings.append(
                f"Bogey: avg {bogey['avg_points']} pts vs {name}"
            )

    avg_opp_quality = sum(opp_quality_scores) / len(opp_quality_scores) if opp_quality_scores else 5.0
    if avg_opp_quality >= 7:
        run_q = "excellent"
    elif avg_opp_quality >= 5.5:
        run_q = "good"
    elif avg_opp_quality >= 4:
        run_q = "mixed"
    else:
        run_q = "tough"
    fixture_run = f"{avg_opp_quality:.1f}/10 ({run_q})"

    # --- Rolling xGI form (more predictive than FPL form) ---
    xgi_data = await form.rolling_xgi(player_id, window=5)
    xgi_per_90 = xgi_data.get("xgi_per_90", 0)
    xgi_trend = xgi_data.get("trend", "unknown")

    # Use rolling xGI as form weight instead of raw FPL form
    # Scale xgi_per_90 to a 0-10 range: 0.5 xGI/90 = elite (10), 0.0 = floor (0)
    xgi_score = min(xgi_per_90 / 0.05, 10) if xgi_per_90 > 0 else player_form

    # --- Venue context for upcoming fixtures ---
    splits = await venue_splits(player_id)
    home_ppg = splits["home"]["ppg"]
    away_ppg = splits["away"]["ppg"]
    venue_bias = splits["venue_bias"]

    # Check if upcoming fixtures match player's venue strength
    home_count = sum(1 for f in upcoming_fixtures[:5] if f["team_h"] == team_id)
    away_count = len(upcoming_fixtures[:5]) - home_count

    if venue_bias == "strong_home" and away_count >= 3:
        context_warnings.append(
            f"Strong home performer ({home_ppg} PPG) but {away_count}/5 fixtures away ({away_ppg} PPG)"
        )
    elif venue_bias == "strong_away" and home_count >= 3:
        # This is actually good — unusual but let's not warn
        pass

    # --- Composite score ---
    composite = (
        total_xp * 3.0
        + xgi_score * 10.0  # rolling xGI form, not raw FPL form
        + avg_opp_quality * 5.0  # opponent quality, not raw FDR
        + per_million * 15.0
    )

    # Penalty for context warnings (bogey teams in run)
    bogey_count = sum(1 for w in context_warnings if w.startswith("Bogey"))
    composite -= bogey_count * 8

    result: dict[str, Any] = {
        "xpts": total_xp,
        "per_million": per_million,
        "form": player_form,
        "xgi_per_90": round(xgi_per_90, 3),
        "xgi_trend": xgi_trend,
        "fixture_run": fixture_run,
        "composite": round(composite, 2),
        "per_gw": xp_data["per_gw"],
    }

    if context_warnings:
        result["warnings"] = context_warnings

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

    # Outcome range
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

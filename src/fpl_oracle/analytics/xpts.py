"""Expected-points engine: xG/xA-based per-gameweek projections.

v2: Uses split attack/defence FDR, Poisson xCS, rolling xGI form,
opponent scoring rate, and bogey-team adjustments.
"""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.analytics.clean_sheets import xcs_from_xga
from fpl_oracle.log import get_logger

log = get_logger(__name__)

# Points per goal by position (element_type)
_GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}
_ASSIST_PTS = 3
_CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}


def _minutes_probability(minutes: int, starts: int, total_gws: int) -> float:
    if total_gws == 0:
        return 0.0
    mpg = minutes / max(total_gws, 1)
    if mpg >= 80:
        return 0.95
    if mpg >= 60:
        return 0.85
    if mpg >= 40:
        return 0.65
    if mpg >= 20:
        return 0.40
    return 0.15


async def calculate_team_strength() -> dict[int, dict[str, int]]:
    """Return {team_id: {att_h, att_a, def_h, def_a, overall_h, overall_a}}."""
    rows = await db.fetch_all(
        "SELECT id, strength_attack_home, strength_attack_away, "
        "strength_defence_home, strength_defence_away, "
        "strength_overall_home, strength_overall_away FROM teams"
    )
    return {
        r["id"]: {
            "att_h": r["strength_attack_home"] or 1200,
            "att_a": r["strength_attack_away"] or 1200,
            "def_h": r["strength_defence_home"] or 1200,
            "def_a": r["strength_defence_away"] or 1200,
            "overall_h": r["strength_overall_home"] or 1200,
            "overall_a": r["strength_overall_away"] or 1200,
        }
        for r in rows
    }


async def _get_opponent_scoring_rate(opp_id: int) -> float:
    """Opponent's average goals scored per game (from team_results)."""
    row = await db.fetch_one(
        "SELECT AVG(goals_for) AS avg_gf FROM team_results WHERE team_id = $1",
        opp_id,
    )
    if row and row["avg_gf"] is not None:
        return float(row["avg_gf"])
    return 1.3  # league average fallback


async def _get_opponent_conceding_rate(opp_id: int) -> float:
    """Opponent's average goals conceded per game (from team_results)."""
    row = await db.fetch_one(
        "SELECT AVG(goals_against) AS avg_ga FROM team_results WHERE team_id = $1",
        opp_id,
    )
    if row and row["avg_ga"] is not None:
        return float(row["avg_ga"])
    return 1.3  # league average fallback


async def _get_bogey_factor(player_id: int, opp_id: int) -> float:
    """Adjustment factor based on player's historical record vs opponent."""
    row = await db.fetch_one(
        "SELECT is_bogey_team, is_favourite, avg_points FROM player_opponent_history "
        "WHERE player_id = $1 AND opponent_id = $2",
        player_id, opp_id,
    )
    if not row:
        return 1.0
    if row["is_bogey_team"]:
        return 0.80  # 20% reduction
    if row["is_favourite"]:
        return 1.15  # 15% boost
    return 1.0


async def _get_rolling_xgi_factor(player_id: int, season_xg: float, season_xa: float, season_mins: int) -> float:
    """Adjustment factor from rolling 5-GW xGI trend vs season rate.

    If recent xGI/90 is higher than season average, player is in hot form.
    """
    from fpl_oracle.analytics.form import rolling_xgi

    rolling = await rolling_xgi(player_id, window=5)
    recent_per90 = rolling.get("xgi_per_90", 0)
    season_per90 = rolling.get("season_xgi_per_90", 0)

    if season_per90 <= 0 or recent_per90 <= 0:
        return 1.0

    ratio = recent_per90 / season_per90
    # Clamp between 0.8 and 1.2 to avoid extreme swings
    return max(0.8, min(1.2, ratio))


async def calculate_expected_points(
    player: dict[str, Any],
    fixtures: list[dict[str, Any]],
    team_strengths: dict[int, dict[str, int]],
    current_gw: int,
) -> dict[str, Any]:
    """Calculate multi-GW expected points for a player.

    v2 improvements:
    - Split FDR: attack difficulty for FWD/MID, defence difficulty for GK/DEF
    - Poisson xCS: real clean sheet probability from opponent scoring rate
    - Rolling xGI: recent form adjustment based on 5-GW xGI/90 trend
    - Bogey teams: historical opponent adjustment
    - Opponent form: scoring/conceding rate adjustments
    """
    minutes = player.get("minutes", 0) or 0
    if minutes < 90:
        return {"total": 0.0, "per_gw": [], "per_million": 0.0}

    elem = player["element_type"]
    team_id = player["team_id"]
    player_id = player["id"]
    xg = float(player.get("expected_goals", 0) or 0)
    xa = float(player.get("expected_assists", 0) or 0)

    # Per-90 rates
    xg_per90 = (xg / minutes) * 90
    xa_per90 = (xa / minutes) * 90

    # Contribution to expected points
    xg_contribution = xg_per90 * _GOAL_PTS.get(elem, 4)
    xa_contribution = xa_per90 * _ASSIST_PTS

    # Count finished GWs for minutes probability
    finished = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM events WHERE finished = TRUE"
    )
    total_gws = finished["n"] if finished else 1
    mins_prob = _minutes_probability(minutes, player.get("starts", 0) or 0, total_gws)

    ts = team_strengths.get(team_id, {})
    price = (player.get("now_cost", 50) or 50) / 10

    # Rolling xGI form adjustment
    xgi_factor = await _get_rolling_xgi_factor(player_id, xg, xa, minutes)

    # Bonus estimation from BPS
    bps = float(player.get("bps", 0) or 0)
    bonus_per_gw = (bps / max(total_gws, 1)) * 0.3 if total_gws else 0

    per_gw: list[dict[str, Any]] = []
    total = 0.0

    for fix in fixtures:
        is_home = fix["team_h"] == team_id
        opp_id = fix["team_a"] if is_home else fix["team_h"]

        # --- Split FDR ---
        # For attackers (MID/FWD): use opponent's conceding rate (attack difficulty)
        # For defenders (GK/DEF): use opponent's scoring rate (defence difficulty)
        opp_conceding = await _get_opponent_conceding_rate(opp_id)
        opp_scoring = await _get_opponent_scoring_rate(opp_id)

        if elem in (3, 4):  # MID/FWD — care about how much opponent concedes
            # Higher conceding = easier to score against = lower difficulty
            att_diff_factor = 0.85 + (opp_conceding - 1.0) * 0.12
        else:
            att_diff_factor = 1.0

        # --- Poisson xCS (replaces old simple CS probability) ---
        home_adj = 0.12 if is_home else -0.08
        cs_prob = xcs_from_xga(opp_scoring, home_adj)
        cs_pts = cs_prob * _CS_PTS.get(elem, 0)

        if elem in (1, 2):  # GK/DEF — care about opponent's attack
            # Lower opponent scoring = better for defence
            def_diff_factor = 1.15 - opp_scoring * 0.12
        else:
            def_diff_factor = 1.0

        # Combined difficulty factor
        diff_factor = att_diff_factor if elem in (3, 4) else def_diff_factor

        # Home/away factor
        home_factor = 1.1 if is_home else 0.95

        # Bogey team adjustment
        bogey_factor = await _get_bogey_factor(player_id, opp_id)

        # Per-GW expected points
        gw_xp = (
            (2.0 + xg_contribution + xa_contribution + cs_pts + bonus_per_gw)
            * diff_factor
            * home_factor
            * mins_prob
            * xgi_factor
            * bogey_factor
        )
        gw_xp = round(gw_xp, 2)
        total += gw_xp

        per_gw.append({
            "gw": fix.get("event"),
            "opponent": opp_id,
            "home": is_home,
            "xpts": gw_xp,
            "cs_prob": round(cs_prob, 3),
            "bogey_adj": round(bogey_factor, 2) if bogey_factor != 1.0 else None,
        })

    total = round(total, 2)
    per_million = round(total / price, 2) if price > 0 else 0.0

    return {
        "total": total,
        "per_gw": per_gw,
        "per_million": per_million,
        "xgi_form_factor": round(xgi_factor, 2),
    }

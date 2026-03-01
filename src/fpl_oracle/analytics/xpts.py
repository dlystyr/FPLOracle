"""Expected-points engine: xG/xA-based per-gameweek projections."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
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


def _cs_probability(
    team_def_strength: int,
    opp_att_strength: int,
) -> float:
    base = 0.30
    factor = (team_def_strength - opp_att_strength) / 100 * 0.15
    return max(0.05, min(0.60, base + factor))


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


async def calculate_expected_points(
    player: dict[str, Any],
    fixtures: list[dict[str, Any]],
    team_strengths: dict[int, dict[str, int]],
    current_gw: int,
) -> dict[str, Any]:
    """Calculate multi-GW expected points for a player.

    Returns dict with total xpts plus per-GW breakdown.
    """
    minutes = player.get("minutes", 0) or 0
    if minutes < 90:
        return {"total": 0.0, "per_gw": [], "per_million": 0.0}

    elem = player["element_type"]
    team_id = player["team_id"]
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

    per_gw: list[dict[str, Any]] = []
    total = 0.0

    for fix in fixtures:
        is_home = fix["team_h"] == team_id
        opp_id = fix["team_a"] if is_home else fix["team_h"]
        difficulty = fix["team_h_difficulty"] if not is_home else fix["team_a_difficulty"]
        # Use opponent's difficulty rating for this player's team
        if is_home:
            difficulty = fix.get("team_a_difficulty", 3)
        else:
            difficulty = fix.get("team_h_difficulty", 3)

        opp_ts = team_strengths.get(opp_id, {})
        def_str = ts.get("def_h" if is_home else "def_a", 1200)
        opp_att = opp_ts.get("att_a" if is_home else "att_h", 1200)

        cs_prob = _cs_probability(def_str, opp_att)
        cs_pts = cs_prob * _CS_PTS.get(elem, 0)

        # Bonus estimation from BPS
        bps = float(player.get("bps", 0) or 0)
        bonus_per_gw = (bps / max(total_gws, 1)) * 0.3 if total_gws else 0

        # Difficulty and home/away adjustments
        diff_factor = 1.0 + (3 - difficulty) * 0.1
        home_factor = 1.1 if is_home else 0.95

        gw_xp = (
            (2.0 + xg_contribution + xa_contribution + cs_pts + bonus_per_gw)
            * diff_factor
            * home_factor
            * mins_prob
        )
        gw_xp = round(gw_xp, 2)
        total += gw_xp

        per_gw.append({
            "gw": fix.get("event"),
            "opponent": opp_id,
            "home": is_home,
            "difficulty": difficulty,
            "xpts": gw_xp,
        })

    total = round(total, 2)
    per_million = round(total / price, 2) if price > 0 else 0.0

    return {"total": total, "per_gw": per_gw, "per_million": per_million}

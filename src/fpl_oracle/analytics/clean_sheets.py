"""Clean sheet probability via Poisson model from opponent xGA."""

from __future__ import annotations

import math
from typing import Any

from fpl_oracle import db
from fpl_oracle.log import get_logger

log = get_logger(__name__)


def poisson_prob(lam: float, k: int) -> float:
    """P(X = k) for Poisson distribution with mean lambda."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def xcs_from_xga(opponent_xga_per90: float, home_advantage: float = 0.0) -> float:
    """Expected clean sheet probability.

    Uses Poisson P(goals=0) where lambda = opponent's xGA/90 adjusted
    for home/away advantage.
    """
    # Adjust xGA for venue: home teams concede ~15% less, away ~10% more
    lam = opponent_xga_per90 * (1.0 - home_advantage)
    # P(opponent scores 0 goals)
    return poisson_prob(lam, 0)


async def team_xcs(team_id: int, num_fixtures: int = 5) -> list[dict[str, Any]]:
    """Calculate xCS for each upcoming fixture for a team.

    Uses opponent's goals conceded rate as proxy for xGA since we don't
    have per-match xGA from the FPL API.
    """
    # Get team's defensive strength
    team = await db.fetch_one(
        "SELECT short_name, strength_defence_home, strength_defence_away "
        "FROM teams WHERE id = $1",
        team_id,
    )
    if not team:
        return []

    # Upcoming fixtures
    fixtures = await db.fetch_all(
        "SELECT f.event, f.team_h, f.team_a, f.team_h_difficulty, "
        "f.team_a_difficulty, t2.short_name AS opp_name, t2.id AS opp_id "
        "FROM fixtures f "
        "JOIN teams t2 ON t2.id = CASE WHEN f.team_h = $1 THEN f.team_a ELSE f.team_h END "
        "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
        "ORDER BY f.event LIMIT $2",
        team_id,
        num_fixtures,
    )

    results = []
    for fix in fixtures:
        is_home = fix["team_h"] == team_id
        opp_id = fix["opp_id"]

        # Opponent's scoring rate from team_results
        opp_scoring = await db.fetch_one(
            "SELECT AVG(goals_for) AS avg_gf, COUNT(*) AS games "
            "FROM team_results WHERE team_id = $1",
            opp_id,
        )

        if opp_scoring and opp_scoring["games"] and opp_scoring["games"] >= 3:
            opp_xga_rate = float(opp_scoring["avg_gf"])
        else:
            # Fallback: use FDR-based estimate
            diff = fix["team_h_difficulty"] if is_home else fix["team_a_difficulty"]
            opp_xga_rate = 0.8 + (diff - 1) * 0.25  # FDR 1→0.8, FDR 5→1.8

        home_adj = 0.12 if is_home else -0.08
        cs_prob = xcs_from_xga(opp_xga_rate, home_adj)

        results.append({
            "gw": fix["event"],
            "opponent": fix["opp_name"],
            "home": is_home,
            "opp_goals_rate": round(opp_xga_rate, 2),
            "xcs": round(cs_prob, 3),
            "xcs_pct": f"{cs_prob * 100:.1f}%",
        })

    return results


async def player_xcs_value(
    player: dict[str, Any], upcoming_fixtures: list[dict[str, Any]]
) -> float:
    """Estimate clean sheet points contribution for a player.

    Only relevant for GK (4pts) and DEF (4pts), MID (1pt).
    """
    elem = player.get("element_type", 4)
    cs_pts = {1: 4, 2: 4, 3: 1, 4: 0}.get(elem, 0)
    if cs_pts == 0:
        return 0.0

    team_id = player["team_id"]
    xcs_data = await team_xcs(team_id, len(upcoming_fixtures))

    total = 0.0
    for xcs in xcs_data:
        total += xcs["xcs"] * cs_pts

    return round(total, 2)

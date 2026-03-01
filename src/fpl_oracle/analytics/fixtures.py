"""Fixture analysis: difficulty ratings, DGW/BGW detection, team outlook."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.log import get_logger

log = get_logger(__name__)


def _run_quality(avg_diff: float) -> str:
    if avg_diff <= 2.5:
        return "excellent"
    if avg_diff <= 3.0:
        return "good"
    if avg_diff <= 3.5:
        return "mixed"
    return "tough"


async def team_fixture_outlook(
    team_id: int, num_fixtures: int = 5
) -> dict[str, Any]:
    """Upcoming fixture difficulty for a team."""
    team = await db.fetch_one("SELECT short_name FROM teams WHERE id = $1", team_id)
    team_name = team["short_name"] if team else "???"

    fixtures = await db.fetch_all(
        "SELECT f.event, f.team_h, f.team_a, f.team_h_difficulty, "
        "f.team_a_difficulty, f.kickoff_time, t2.short_name AS opp_name "
        "FROM fixtures f "
        "JOIN teams t2 ON t2.id = CASE WHEN f.team_h = $1 THEN f.team_a ELSE f.team_h END "
        "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
        "ORDER BY f.event, f.kickoff_time LIMIT $2",
        team_id,
        num_fixtures,
    )

    items: list[dict[str, Any]] = []
    total_diff = 0.0
    for f in fixtures:
        is_home = f["team_h"] == team_id
        diff = f["team_h_difficulty"] if is_home else f["team_a_difficulty"]
        total_diff += diff
        items.append({
            "gw": f["event"],
            "opponent": f["opp_name"],
            "home": is_home,
            "difficulty": diff,
            "kickoff": f["kickoff_time"].isoformat() if f["kickoff_time"] else None,
        })

    avg = round(total_diff / len(items), 2) if items else 0.0

    return {
        "team": team_name,
        "avg_difficulty": avg,
        "run_quality": _run_quality(avg),
        "fixtures": items,
    }


async def all_teams_outlook(num_fixtures: int = 5) -> list[dict[str, Any]]:
    """All teams ranked by avg upcoming fixture difficulty (easiest first)."""
    teams = await db.fetch_all("SELECT id, short_name FROM teams ORDER BY id")
    outlooks = []
    for t in teams:
        outlook = await team_fixture_outlook(t["id"], num_fixtures)
        outlooks.append(outlook)
    outlooks.sort(key=lambda x: x["avg_difficulty"])
    return outlooks


async def detect_dgw_bgw() -> dict[str, Any]:
    """Detect double and blank gameweeks from fixture data."""
    # DGW: teams with >1 fixture in a single GW
    dgw_rows = await db.fetch_all(
        "SELECT f.event, t.short_name, COUNT(*) AS fixture_count "
        "FROM fixtures f "
        "JOIN teams t ON t.id IN (f.team_h, f.team_a) "
        "WHERE NOT f.finished "
        "GROUP BY f.event, t.short_name "
        "HAVING COUNT(*) > 1 "
        "ORDER BY f.event"
    )

    dgw: dict[int, list[str]] = {}
    for r in dgw_rows:
        gw = r["event"]
        dgw.setdefault(gw, []).append(r["short_name"])

    # BGW: GWs with fewer than 20 teams playing
    bgw_rows = await db.fetch_all(
        "SELECT e.id AS event, "
        "20 - COUNT(DISTINCT t.id) AS missing_teams "
        "FROM events e "
        "LEFT JOIN fixtures f ON f.event = e.id AND NOT f.finished "
        "LEFT JOIN teams t ON t.id IN (f.team_h, f.team_a) "
        "WHERE NOT e.finished AND e.id IS NOT NULL "
        "GROUP BY e.id "
        "HAVING COUNT(DISTINCT t.id) < 20 "
        "ORDER BY e.id"
    )

    bgw: dict[int, int] = {}
    for r in bgw_rows:
        if r["missing_teams"] and r["missing_teams"] > 0:
            bgw[r["event"]] = r["missing_teams"]

    return {
        "double_gameweeks": {gw: teams for gw, teams in dgw.items()},
        "blank_gameweeks": {gw: missing for gw, missing in bgw.items()},
    }


async def fixture_congestion(team_id: int, window_gws: int = 6) -> float:
    """Count fixtures in the next N gameweeks (>1.2 per GW = congested)."""
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM fixtures "
        "WHERE (team_h = $1 OR team_a = $1) AND NOT finished "
        "AND event <= (SELECT MIN(id) + $2 FROM events WHERE NOT finished)",
        team_id,
        window_gws,
    )
    count = row["n"] if row else 0
    return round(count / max(window_gws, 1), 2)

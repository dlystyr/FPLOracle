"""Chip timing optimization: TC, BB, FH, WC planning."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.analytics.fixtures import detect_dgw_bgw
from fpl_oracle.log import get_logger

log = get_logger(__name__)

# Premium teams that boost TC/BB value
PREMIUM_TEAMS = {"MCI", "ARS", "LIV", "CHE", "MUN", "TOT"}


async def analyze_triple_captain(remaining_gws: int = 15) -> list[dict[str, Any]]:
    """Find best Triple Captain opportunities.

    Priority: DGW with premium team assets at home with easy fixtures.
    """
    dgw_bgw = await detect_dgw_bgw()
    dgw_data = dgw_bgw["double_gameweeks"]

    opportunities = []

    # DGW opportunities
    for gw, teams in dgw_data.items():
        premium_dgw = [t for t in teams if t in PREMIUM_TEAMS]
        score = len(teams) * 5 + len(premium_dgw) * 15

        if premium_dgw:
            confidence = "high"
        elif len(teams) >= 4:
            confidence = "medium"
        else:
            confidence = "low"

        opportunities.append({
            "gameweek": gw,
            "type": "DGW",
            "dgw_teams": teams,
            "premium_teams": premium_dgw,
            "score": score,
            "confidence": confidence,
            "reason": f"DGW with {len(teams)} teams" + (
                f" incl. {', '.join(premium_dgw)}" if premium_dgw else ""
            ),
        })

    # Fallback: GWs with easy home fixtures for premium teams
    if not opportunities:
        easy_gws = await db.fetch_all(
            "SELECT f.event, t.short_name, f.team_h_difficulty "
            "FROM fixtures f "
            "JOIN teams t ON f.team_h = t.id "
            "WHERE NOT f.finished AND f.team_h_difficulty <= 2 "
            "AND t.short_name = ANY($1::text[]) "
            "ORDER BY f.event LIMIT 5",
            list(PREMIUM_TEAMS),
        )
        for row in easy_gws:
            opportunities.append({
                "gameweek": row["event"],
                "type": "easy_home",
                "premium_teams": [row["short_name"]],
                "score": 15,
                "confidence": "low",
                "reason": f"{row['short_name']} home with FDR {row['team_h_difficulty']}",
            })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities


async def analyze_bench_boost(remaining_gws: int = 15) -> list[dict[str, Any]]:
    """Find best Bench Boost opportunities.

    Best when many teams have double fixtures (more players play twice).
    """
    dgw_bgw = await detect_dgw_bgw()
    dgw_data = dgw_bgw["double_gameweeks"]

    opportunities = []
    for gw, teams in dgw_data.items():
        team_count = len(teams)
        if team_count >= 8:
            confidence = "high"
        elif team_count >= 5:
            confidence = "medium"
        else:
            confidence = "low"

        opportunities.append({
            "gameweek": gw,
            "dgw_team_count": team_count,
            "dgw_teams": teams,
            "score": team_count * 10,
            "confidence": confidence,
            "reason": f"DGW with {team_count} teams doubling",
        })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities


async def analyze_free_hit(remaining_gws: int = 15) -> list[dict[str, Any]]:
    """Find best Free Hit opportunities.

    Best for BGWs (few teams playing) or large DGWs.
    """
    dgw_bgw = await detect_dgw_bgw()
    bgw_data = dgw_bgw["blank_gameweeks"]
    dgw_data = dgw_bgw["double_gameweeks"]

    opportunities = []

    # BGW opportunities
    for gw, missing in bgw_data.items():
        score = missing * 10
        opportunities.append({
            "gameweek": gw,
            "type": "BGW",
            "missing_teams": missing,
            "score": score,
            "confidence": "high" if missing >= 6 else "medium",
            "reason": f"BGW with {missing} teams not playing",
        })

    # DGW opportunities
    for gw, teams in dgw_data.items():
        score = len(teams) * 8
        opportunities.append({
            "gameweek": gw,
            "type": "DGW",
            "dgw_teams": teams,
            "score": score,
            "confidence": "medium",
            "reason": f"DGW with {len(teams)} teams doubling — load up",
        })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities


async def analyze_wildcard() -> list[dict[str, Any]]:
    """Find best Wildcard windows.

    Best before a fixture swing where many teams' difficulty changes.
    """
    # Look for GWs where fixture difficulty shifts significantly
    teams = await db.fetch_all("SELECT id, short_name FROM teams")
    swing_gws: dict[int, int] = {}

    for t in teams:
        fixtures = await db.fetch_all(
            "SELECT f.event, "
            "CASE WHEN f.team_h = $1 THEN f.team_h_difficulty ELSE f.team_a_difficulty END AS diff "
            "FROM fixtures f "
            "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
            "ORDER BY f.event LIMIT 10",
            t["id"],
        )
        # Find GW where difficulty swings (from hard to easy or vice versa)
        for i in range(1, len(fixtures)):
            prev_diff = fixtures[i - 1]["diff"]
            curr_diff = fixtures[i]["diff"]
            if prev_diff and curr_diff and abs(prev_diff - curr_diff) >= 2:
                gw = fixtures[i]["event"]
                swing_gws[gw] = swing_gws.get(gw, 0) + 1

    opportunities = [
        {
            "gameweek": gw,
            "teams_swinging": count,
            "score": count * 5,
            "confidence": "high" if count >= 6 else "medium" if count >= 3 else "low",
            "reason": f"{count} teams have fixture difficulty swing at GW{gw}",
        }
        for gw, count in swing_gws.items()
    ]
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities[:5]


async def chip_calendar(remaining_gws: int = 15) -> dict[str, Any]:
    """Full chip usage calendar: recommended GW for each chip."""
    tc = await analyze_triple_captain(remaining_gws)
    bb = await analyze_bench_boost(remaining_gws)
    fh = await analyze_free_hit(remaining_gws)
    wc = await analyze_wildcard()

    return {
        "triple_captain": tc[0] if tc else {"reason": "No clear opportunity yet"},
        "bench_boost": bb[0] if bb else {"reason": "No clear opportunity yet"},
        "free_hit": fh[0] if fh else {"reason": "No clear opportunity yet"},
        "wildcard": wc[0] if wc else {"reason": "Use before major fixture swing"},
        "strategy": (
            "Save chips for DGW/BGW windows. "
            "TC on a premium captain in a DGW. "
            "BB when your full bench also doubles. "
            "FH to navigate BGWs. "
            "WC before a major fixture swing."
        ),
    }

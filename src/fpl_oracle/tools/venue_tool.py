"""Home/away splits and next GW predictor tools."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.analytics.venue import venue_splits
from fpl_oracle.analytics import xpts, scoring
from fpl_oracle.models import POS_MAP, POS_REVERSE
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def home_away_splits(
    player_id: Annotated[int | None, "Player ID"] = None,
    name: Annotated[str | None, "Player name"] = None,
) -> dict:
    """Home vs away performance breakdown. Shows PPG, goals, assists, xG, xA split by venue. Some players score 80%+ of their goals at home — critical for captain picks and transfer timing."""
    if player_id:
        row = await db.fetch_one(
            "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost "
            "FROM players p JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
            player_id,
        )
    elif name:
        row = await db.fetch_one(
            "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost "
            "FROM players p JOIN teams t ON p.team_id = t.id "
            "WHERE LOWER(p.web_name) = $1 LIMIT 1",
            name.lower(),
        )
    else:
        return {"error": "Provide player_id or name"}

    if not row:
        return {"error": "Player not found"}

    splits = await venue_splits(row["id"])

    # Check next fixture venue
    next_fix = await db.fetch_one(
        "SELECT f.team_h, t2.short_name AS opp_name "
        "FROM fixtures f "
        "JOIN teams t2 ON t2.id = CASE WHEN f.team_h = $1 THEN f.team_a ELSE f.team_h END "
        "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
        "ORDER BY f.event LIMIT 1",
        row["id"],
    )

    # Resolve team_id
    team_row = await db.fetch_one(
        "SELECT id FROM teams WHERE short_name = $1", row["short_name"]
    )
    team_id = team_row["id"] if team_row else None
    if next_fix and team_id:
        is_home = next_fix["team_h"] == team_id
        relevant_ppg = splits["home"]["ppg"] if is_home else splits["away"]["ppg"]
        venue_note = f"Next: {'HOME' if is_home else 'AWAY'} vs {next_fix['opp_name']} — historical PPG at this venue: {relevant_ppg}"
    else:
        venue_note = None

    return {
        "player": row["web_name"],
        "team": row["short_name"],
        "pos": POS_MAP.get(row["element_type"], "???"),
        **splits,
        "venue_note": venue_note,
    }


@mcp.tool()
async def next_gw_picks(
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> list[dict]:
    """Who will score highest THIS gameweek? Single-fixture prediction weighted by venue, opponent form, recent momentum, and fixture difficulty. Different from rank_players which optimizes over 5 GWs."""
    current_gw_row = await db.fetch_one(
        "SELECT id FROM events WHERE is_next = TRUE OR is_current = TRUE ORDER BY id LIMIT 1"
    )
    current_gw = current_gw_row["id"] if current_gw_row else 1

    conditions = ["p.status = 'a'", "p.minutes >= 180"]
    params: list = []
    idx = 1

    if position:
        et = POS_REVERSE.get(position.upper())
        if et:
            conditions.append(f"p.element_type = ${idx}")
            params.append(et)
            idx += 1

    rows = await db.fetch_all(
        f"SELECT p.*, t.short_name FROM players p "
        f"JOIN teams t ON p.team_id = t.id "
        f"WHERE {' AND '.join(conditions)} "
        f"ORDER BY p.form DESC LIMIT ${idx}",
        *params, min(limit * 5, 80),
    )

    team_strengths = await xpts.calculate_team_strength()
    predictions: list[dict] = []

    for row in rows:
        # Get ONLY the next fixture
        fix = await db.fetch_all(
            "SELECT f.*, t2.short_name AS opp_name FROM fixtures f "
            "JOIN teams t2 ON t2.id = CASE WHEN f.team_h = $1 THEN f.team_a ELSE f.team_h END "
            "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
            "ORDER BY f.event LIMIT 1",
            row["team_id"],
        )
        if not fix:
            continue

        next_fix = fix[0]
        is_home = next_fix["team_h"] == row["team_id"]
        opp_name = next_fix["opp_name"]
        diff = next_fix.get("team_a_difficulty", 3) if is_home else next_fix.get("team_h_difficulty", 3)

        # Single-GW xPts
        xp_data = await xpts.calculate_expected_points(
            row, fix, team_strengths, current_gw
        )
        gw_xpts = xp_data["per_gw"][0]["xpts"] if xp_data.get("per_gw") else xp_data["total"]

        # Venue adjustment from historical splits
        splits = await venue_splits(row["id"])
        venue_ppg = splits["home"]["ppg"] if is_home else splits["away"]["ppg"]
        overall_ppg = float(row.get("points_per_game", 0) or 0)
        if overall_ppg > 0 and venue_ppg > 0:
            venue_factor = venue_ppg / overall_ppg
        else:
            venue_factor = 1.0

        # Opponent form adjustment
        opp_id = next_fix["team_a"] if is_home else next_fix["team_h"]
        opp_form = await db.fetch_one(
            "SELECT AVG(goals_against) AS avg_ga FROM team_results "
            "WHERE team_id = $1 ORDER BY event DESC LIMIT 5",
            opp_id,
        )
        # If opponent concedes a lot, boost; if tight, reduce
        opp_ga = float(opp_form["avg_ga"]) if opp_form and opp_form["avg_ga"] else 1.3
        opp_factor = 0.85 + (opp_ga - 1.0) * 0.15  # >1.3 goals conceded = boost

        # Bogey check
        bogey = await db.fetch_one(
            "SELECT is_bogey_team, is_favourite, avg_points FROM player_opponent_history "
            "WHERE player_id = $1 AND opponent_id = $2",
            row["id"], opp_id,
        )
        bogey_factor = 1.0
        bogey_note = None
        if bogey:
            if bogey["is_bogey_team"]:
                bogey_factor = 0.75
                bogey_note = f"Bogey team (avg {bogey['avg_points']} pts vs them)"
            elif bogey["is_favourite"]:
                bogey_factor = 1.2
                bogey_note = f"Favourite opponent (avg {bogey['avg_points']} pts vs them)"

        # Recent momentum (last 3 GW form)
        form = float(row.get("form", 0) or 0)
        momentum_factor = 0.9 + (form / 10) * 0.2  # form 5 = 1.0, form 10 = 1.1

        # Final adjusted score
        adjusted_xpts = round(
            gw_xpts * venue_factor * opp_factor * bogey_factor * momentum_factor, 2
        )

        predictions.append({
            "id": row["id"],
            "name": row["web_name"],
            "team": row["short_name"],
            "pos": POS_MAP.get(row["element_type"], "???"),
            "price": round(row["now_cost"] / 10, 1),
            "opponent": f"{opp_name} ({'H' if is_home else 'A'})",
            "difficulty": diff,
            "raw_xpts": round(gw_xpts, 2),
            "adjusted_xpts": adjusted_xpts,
            "form": form,
            "venue_ppg": venue_ppg,
            "venue_factor": round(venue_factor, 2),
            "opp_goals_conceded": round(opp_ga, 2),
            "bogey_note": bogey_note,
        })

    predictions.sort(key=lambda x: x["adjusted_xpts"], reverse=True)
    return predictions[:limit]

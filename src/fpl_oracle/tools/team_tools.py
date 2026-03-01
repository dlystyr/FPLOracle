"""Team tools: team form analysis, season fixture ticker."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.analytics.form import team_form
from fpl_oracle.analytics.fixtures import team_fixture_outlook, all_teams_outlook
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def get_team_form(
    team: Annotated[str, "Team short name e.g. ARS, LIV, MCI"],
) -> dict:
    """Team's recent form: W/D/L record, goals for/against, clean sheets, attack/defence strength."""
    team_row = await db.fetch_one(
        "SELECT id, name, short_name, strength, "
        "strength_attack_home, strength_attack_away, "
        "strength_defence_home, strength_defence_away "
        "FROM teams WHERE UPPER(short_name) = $1",
        team.upper(),
    )
    if not team_row:
        return {"error": f"Team '{team}' not found"}

    form_data = await team_form(team_row["id"])

    # Attack/defence ratings
    att_home = team_row.get("strength_attack_home", 0) or 0
    att_away = team_row.get("strength_attack_away", 0) or 0
    def_home = team_row.get("strength_defence_home", 0) or 0
    def_away = team_row.get("strength_defence_away", 0) or 0

    # Scoriness proxy: goals scored rate from results
    scoring_row = await db.fetch_one(
        "SELECT AVG(goals_for) AS avg_gf, AVG(goals_against) AS avg_ga, "
        "COUNT(*) FILTER (WHERE clean_sheet) AS cs_count, COUNT(*) AS games "
        "FROM team_results WHERE team_id = $1",
        team_row["id"],
    )

    scoriness = round(float(scoring_row["avg_gf"]), 2) if scoring_row and scoring_row["avg_gf"] else 0
    porosity = round(float(scoring_row["avg_ga"]), 2) if scoring_row and scoring_row["avg_ga"] else 0
    cs_rate = 0.0
    if scoring_row and scoring_row["games"]:
        cs_rate = round(scoring_row["cs_count"] / scoring_row["games"] * 100, 1)

    return {
        "team": team_row["short_name"],
        "full_name": team_row["name"],
        **form_data,
        "strength": {
            "attack_home": att_home,
            "attack_away": att_away,
            "defence_home": def_home,
            "defence_away": def_away,
        },
        "scoriness": scoriness,
        "porosity": porosity,
        "cs_rate_pct": cs_rate,
        "summary": (
            f"{team_row['short_name']}: {form_data['form']} | "
            f"Scoring {scoriness} gpg, conceding {porosity} gpg, "
            f"{cs_rate}% CS rate"
        ),
    }


@mcp.tool()
async def season_ticker(
    num_fixtures: Annotated[int, "Fixtures per team to show (default 8)"] = 8,
    sort_by: Annotated[str, "'attack' (easiest to score against), 'defence' (easiest to keep CS), or 'overall'"] = "overall",
) -> list[dict]:
    """Full season fixture ticker with attack/defence difficulty split. Shows which teams have the best upcoming runs for attackers vs defenders."""
    teams = await db.fetch_all(
        "SELECT id, short_name FROM teams ORDER BY id"
    )

    ticker = []
    for t in teams:
        fixtures = await db.fetch_all(
            "SELECT f.event, f.team_h, f.team_a, "
            "f.team_h_difficulty, f.team_a_difficulty, "
            "t2.short_name AS opp_name, t2.id AS opp_id "
            "FROM fixtures f "
            "JOIN teams t2 ON t2.id = CASE WHEN f.team_h = $1 THEN f.team_a ELSE f.team_h END "
            "WHERE (f.team_h = $1 OR f.team_a = $1) AND NOT f.finished "
            "ORDER BY f.event LIMIT $2",
            t["id"], num_fixtures,
        )

        att_diffs = []
        def_diffs = []
        fixture_list = []

        for f in fixtures:
            is_home = f["team_h"] == t["id"]
            # Overall difficulty
            overall_diff = f["team_a_difficulty"] if is_home else f["team_h_difficulty"]

            # Get opponent's defensive/offensive strength for split analysis
            opp_scoring = await db.fetch_one(
                "SELECT AVG(goals_for) AS avg_gf, AVG(goals_against) AS avg_ga "
                "FROM team_results WHERE team_id = $1",
                f["opp_id"],
            )

            # Attack difficulty: how easy is it to score against this opponent
            # Lower opponent avg_ga = harder to score (better defence)
            opp_ga = float(opp_scoring["avg_ga"]) if opp_scoring and opp_scoring["avg_ga"] else 1.3
            att_diff = round(5 - (opp_ga - 0.5) * 2, 1)  # Invert: higher conceding = lower difficulty
            att_diff = max(1, min(5, att_diff))

            # Defence difficulty: how easy is it to keep a CS against this opponent
            # Lower opponent avg_gf = easier to keep CS
            opp_gf = float(opp_scoring["avg_gf"]) if opp_scoring and opp_scoring["avg_gf"] else 1.3
            def_diff = round(1 + (opp_gf - 0.5) * 2, 1)
            def_diff = max(1, min(5, def_diff))

            att_diffs.append(att_diff)
            def_diffs.append(def_diff)

            fixture_list.append({
                "gw": f["event"],
                "opp": f["opp_name"],
                "home": is_home,
                "overall": overall_diff,
                "att_diff": att_diff,
                "def_diff": def_diff,
            })

        avg_att = round(sum(att_diffs) / len(att_diffs), 2) if att_diffs else 3.0
        avg_def = round(sum(def_diffs) / len(def_diffs), 2) if def_diffs else 3.0
        avg_overall = round((avg_att + avg_def) / 2, 2)

        ticker.append({
            "team": t["short_name"],
            "avg_attack_difficulty": avg_att,
            "avg_defence_difficulty": avg_def,
            "avg_overall": avg_overall,
            "fixtures": fixture_list,
        })

    # Sort
    if sort_by == "attack":
        ticker.sort(key=lambda x: x["avg_attack_difficulty"])
    elif sort_by == "defence":
        ticker.sort(key=lambda x: x["avg_defence_difficulty"])
    else:
        ticker.sort(key=lambda x: x["avg_overall"])

    return ticker

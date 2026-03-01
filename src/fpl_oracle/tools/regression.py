"""Regression tool: xG luck analysis — over/underperformers."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.models import POS_MAP, POS_REVERSE
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def xg_luck(
    player_id: Annotated[int | None, "Specific player ID to analyze"] = None,
    show: Annotated[str, "'overperformers' (sell candidates), 'underperformers' (buy candidates), or 'both'"] = "both",
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    limit: Annotated[int, "Max results per category (default 10)"] = 10,
) -> dict:
    """xG regression analysis. Overperformers are sell candidates (scoring above xG, due to regress). Underperformers are buy candidates (scoring below xG, due for more goals)."""
    if player_id:
        row = await db.fetch_one(
            "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost, "
            "p.goals_scored, p.assists, p.expected_goals, p.expected_assists, "
            "p.minutes, p.form "
            "FROM players p JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
            player_id,
        )
        if not row:
            return {"error": "Player not found"}

        return _analyze_player(row)

    result: dict = {}
    conditions = ["p.minutes >= 450"]
    params: list = []
    idx = 1

    if position:
        et = POS_REVERSE.get(position.upper())
        if et:
            conditions.append(f"p.element_type = ${idx}")
            params.append(et)
            idx += 1

    base_sql = (
        "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost, "
        "p.goals_scored, p.assists, p.expected_goals, p.expected_assists, "
        "p.minutes, p.form, "
        "(p.goals_scored - p.expected_goals) + (p.assists - p.expected_assists) AS overperformance "
        f"FROM players p JOIN teams t ON p.team_id = t.id "
        f"WHERE {' AND '.join(conditions)}"
    )

    if show in ("overperformers", "both"):
        rows = await db.fetch_all(
            f"{base_sql} AND (p.goals_scored - p.expected_goals) + (p.assists - p.expected_assists) > 1 "
            f"ORDER BY overperformance DESC LIMIT ${idx}",
            *params, limit,
        )
        result["overperformers_sell_candidates"] = [_analyze_player(r) for r in rows]

    if show in ("underperformers", "both"):
        rows = await db.fetch_all(
            f"{base_sql} AND (p.goals_scored - p.expected_goals) + (p.assists - p.expected_assists) < -1 "
            f"ORDER BY overperformance ASC LIMIT ${idx}",
            *params, limit,
        )
        result["underperformers_buy_candidates"] = [_analyze_player(r) for r in rows]

    return result


def _analyze_player(row: dict) -> dict:
    goals = row.get("goals_scored", 0) or 0
    assists = row.get("assists", 0) or 0
    xg = float(row.get("expected_goals", 0) or 0)
    xa = float(row.get("expected_assists", 0) or 0)

    g_diff = goals - xg
    a_diff = assists - xa
    total_diff = g_diff + a_diff

    if total_diff > 3:
        verdict = "very_lucky"
        risk = "high regression risk — sell candidate"
    elif total_diff > 1.5:
        verdict = "lucky"
        risk = "medium regression risk"
    elif total_diff > -1.5:
        verdict = "neutral"
        risk = "performing as expected"
    elif total_diff > -3:
        verdict = "unlucky"
        risk = "medium upside — buy candidate"
    else:
        verdict = "very_unlucky"
        risk = "high upside — strong buy candidate"

    return {
        "id": row["id"],
        "name": row["web_name"],
        "team": row["short_name"],
        "pos": POS_MAP.get(row["element_type"], "???"),
        "price": round(row["now_cost"] / 10, 1),
        "goals": goals,
        "xg": round(xg, 2),
        "goals_minus_xg": round(g_diff, 2),
        "assists": assists,
        "xa": round(xa, 2),
        "assists_minus_xa": round(a_diff, 2),
        "total_overperformance": round(total_diff, 2),
        "verdict": verdict,
        "action": risk,
        "form": float(row.get("form", 0) or 0),
    }

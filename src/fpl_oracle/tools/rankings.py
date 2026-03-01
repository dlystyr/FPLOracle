"""Ranking tools: best players, differentials, value picks."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.models import POS_MAP, POS_REVERSE, ScoredPlayer
from fpl_oracle.analytics import xpts, scoring
from fpl_oracle.log import get_logger

log = get_logger(__name__)


async def _get_current_gw() -> int:
    row = await db.fetch_one("SELECT id FROM events WHERE is_current = TRUE LIMIT 1")
    return row["id"] if row else 1


async def _score_players(
    rows: list[dict], team_strengths: dict, current_gw: int, *, refine: bool = False
) -> list[dict]:
    """Score a list of player rows and return sorted ScoredPlayer dicts."""
    scored = []
    for row in rows:
        fixtures = await db.fetch_all(
            "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
            "AND NOT finished ORDER BY event LIMIT 5",
            row["team_id"],
        )
        score_data = await scoring.score_player(
            row, fixtures, team_strengths, current_gw, refine=refine
        )

        sp = ScoredPlayer(
            id=row["id"],
            name=row["web_name"],
            team=row["short_name"],
            pos=POS_MAP.get(row["element_type"], "???"),
            price=round(row["now_cost"] / 10, 1),
            xpts=score_data["xpts"],
            form=float(row.get("form", 0) or 0),
            minutes=row.get("minutes", 0) or 0,
            xg=round(float(row.get("expected_goals", 0) or 0), 2),
            xa=round(float(row.get("expected_assists", 0) or 0), 2),
            fixture_run=score_data.get("fixture_run"),
            score=score_data["composite"],
        )

        result = sp.model_dump(exclude_none=True)
        # Add refinement fields if present
        for key in ("refined_xpts", "momentum", "confidence", "floor", "ceiling",
                     "rotation_risk", "nailed_score"):
            if key in score_data:
                result[key] = score_data[key]

        scored.append(result)

    scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    return scored


@mcp.tool()
async def rank_players(
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
    refine: Annotated[bool, "Apply 2nd-pass refinement with momentum/confidence"] = False,
) -> list[dict]:
    """Rank players by composite score (xPts + form + fixtures + value). Set refine=true for deeper analysis."""
    conditions = ["p.minutes > 90", "p.status = 'a'"]
    params: list = []
    idx = 1

    if position:
        et = POS_REVERSE.get(position.upper())
        if et:
            conditions.append(f"p.element_type = ${idx}")
            params.append(et)
            idx += 1

    where = " AND ".join(conditions)
    sql = (
        f"SELECT p.*, t.short_name FROM players p "
        f"JOIN teams t ON p.team_id = t.id "
        f"WHERE {where} ORDER BY p.form DESC LIMIT ${idx}"
    )
    # Fetch more than needed so scoring can re-rank
    params.append(min(limit * 3, 60))

    rows = await db.fetch_all(sql, *params)
    team_strengths = await xpts.calculate_team_strength()
    current_gw = await _get_current_gw()

    scored = await _score_players(rows, team_strengths, current_gw, refine=refine)
    return scored[:limit]


@mcp.tool()
async def get_differentials(
    max_ownership: Annotated[float, "Max ownership % (default 10)"] = 10.0,
    min_form: Annotated[float, "Min form (default 4)"] = 4.0,
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> list[dict]:
    """Find low-ownership players with good form — differential picks."""
    conditions = [
        "p.selected_by_percent <= $1",
        "p.form >= $2",
        "p.minutes >= 180",
        "p.status = 'a'",
    ]
    params: list = [max_ownership, min_form]
    idx = 3

    if position:
        et = POS_REVERSE.get(position.upper())
        if et:
            conditions.append(f"p.element_type = ${idx}")
            params.append(et)
            idx += 1

    where = " AND ".join(conditions)
    sql = (
        f"SELECT p.*, t.short_name FROM players p "
        f"JOIN teams t ON p.team_id = t.id "
        f"WHERE {where} ORDER BY p.form DESC LIMIT ${idx}"
    )
    params.append(min(limit * 2, 40))

    rows = await db.fetch_all(sql, *params)
    team_strengths = await xpts.calculate_team_strength()
    current_gw = await _get_current_gw()

    scored = await _score_players(rows, team_strengths, current_gw)
    return scored[:limit]


@mcp.tool()
async def get_value_picks(
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    max_price: Annotated[float | None, "Max price in £m"] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> list[dict]:
    """Find best value players (highest xPts per £m)."""
    conditions = ["p.minutes >= 90", "p.status = 'a'"]
    params: list = []
    idx = 1

    if position:
        et = POS_REVERSE.get(position.upper())
        if et:
            conditions.append(f"p.element_type = ${idx}")
            params.append(et)
            idx += 1

    if max_price is not None:
        conditions.append(f"p.now_cost <= ${idx}")
        params.append(int(max_price * 10))
        idx += 1

    where = " AND ".join(conditions)
    sql = (
        f"SELECT p.*, t.short_name FROM players p "
        f"JOIN teams t ON p.team_id = t.id "
        f"WHERE {where} ORDER BY p.form DESC LIMIT ${idx}"
    )
    params.append(min(limit * 3, 60))

    rows = await db.fetch_all(sql, *params)
    team_strengths = await xpts.calculate_team_strength()
    current_gw = await _get_current_gw()

    scored = await _score_players(rows, team_strengths, current_gw)

    # Re-sort by xpts per million
    for s in scored:
        price = s.get("price", 4.0)
        s["per_million"] = round(s.get("xpts", 0) / max(price, 0.1), 2)

    scored.sort(key=lambda x: x.get("per_million", 0), reverse=True)
    return scored[:limit]

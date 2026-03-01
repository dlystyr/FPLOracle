"""Player tools: search, info, compare, enriched stats."""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.models import POS_MAP, POS_REVERSE, PlayerDetail, PlayerRef, CompareRow, EnrichedStats
from fpl_oracle.log import get_logger

log = get_logger(__name__)


def _player_ref(row: dict) -> PlayerRef:
    return PlayerRef(
        id=row["id"],
        name=row["web_name"],
        team=row["short_name"],
        pos=POS_MAP.get(row["element_type"], "???"),
        price=round(row["now_cost"] / 10, 1),
    )


def _player_detail(row: dict) -> PlayerDetail:
    return PlayerDetail(
        id=row["id"],
        name=row["web_name"],
        team=row["short_name"],
        pos=POS_MAP.get(row["element_type"], "???"),
        price=round(row["now_cost"] / 10, 1),
        form=float(row.get("form", 0) or 0),
        points=row.get("total_points", 0) or 0,
        minutes=row.get("minutes", 0) or 0,
        goals=row.get("goals_scored", 0) or 0,
        assists=row.get("assists", 0) or 0,
        cs=row.get("clean_sheets", 0) or 0,
        xg=round(float(row.get("expected_goals", 0) or 0), 2),
        xa=round(float(row.get("expected_assists", 0) or 0), 2),
        ict=round(float(row.get("ict_index", 0) or 0), 1),
        ownership=float(row.get("selected_by_percent", 0) or 0),
        status=row.get("status", "a"),
        news=row.get("news") or None,
    )


_PLAYER_JOIN = (
    "SELECT p.*, t.short_name FROM players p "
    "JOIN teams t ON p.team_id = t.id"
)


@mcp.tool()
async def search_players(
    query: Annotated[str, "Player name to search for"],
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    team: Annotated[str | None, "Filter: team short name e.g. ARS"] = None,
    max_price: Annotated[float | None, "Max price in £m"] = None,
    limit: Annotated[int, "Max results (default 10)"] = 10,
) -> list[dict]:
    """Search FPL players by name. Returns compact results with id, name, team, pos, price, form, points."""
    conditions = ["LOWER(p.web_name) LIKE $1"]
    params: list = [f"%{query.lower()}%"]
    idx = 2

    if position:
        et = POS_REVERSE.get(position.upper())
        if et:
            conditions.append(f"p.element_type = ${idx}")
            params.append(et)
            idx += 1

    if team:
        conditions.append(f"UPPER(t.short_name) = ${idx}")
        params.append(team.upper())
        idx += 1

    if max_price is not None:
        conditions.append(f"p.now_cost <= ${idx}")
        params.append(int(max_price * 10))
        idx += 1

    where = " AND ".join(conditions)
    sql = f"{_PLAYER_JOIN} WHERE {where} ORDER BY p.form DESC LIMIT ${idx}"
    params.append(limit)

    rows = await db.fetch_all(sql, *params)
    return [_player_ref(r).model_dump() for r in rows]


@mcp.tool()
async def get_players(
    ids: Annotated[list[int] | None, "Player IDs to look up"] = None,
    names: Annotated[list[str] | None, "Player names to look up"] = None,
) -> list[dict]:
    """Get detailed info for players by ID or name. Use search_players first to find IDs."""
    results: list[dict] = []

    if ids:
        for pid in ids[:15]:  # cap at 15
            row = await db.fetch_one(f"{_PLAYER_JOIN} WHERE p.id = $1", pid)
            if row:
                results.append(_player_detail(row).model_dump(exclude_none=True))

    if names:
        for name in names[:10]:
            row = await db.fetch_one(
                f"{_PLAYER_JOIN} WHERE LOWER(p.web_name) = $1 "
                "OR LOWER(p.second_name) = $1 LIMIT 1",
                name.lower(),
            )
            if row:
                results.append(_player_detail(row).model_dump(exclude_none=True))

    return results


@mcp.tool()
async def compare_players(
    player_ids: Annotated[list[int], "2-4 player IDs to compare side-by-side"],
) -> list[dict]:
    """Compare players side-by-side with stats and xPts."""
    from fpl_oracle.analytics import xpts, scoring

    team_strengths = await xpts.calculate_team_strength()
    current_gw_row = await db.fetch_one(
        "SELECT id FROM events WHERE is_current = TRUE LIMIT 1"
    )
    current_gw = current_gw_row["id"] if current_gw_row else 1

    results = []
    for pid in player_ids[:4]:
        row = await db.fetch_one(f"{_PLAYER_JOIN} WHERE p.id = $1", pid)
        if not row:
            continue

        fixtures = await db.fetch_all(
            "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
            "AND NOT finished ORDER BY event LIMIT 5",
            row["team_id"],
        )
        score_data = await scoring.score_player(
            row, fixtures, team_strengths, current_gw
        )

        detail = _player_detail(row)
        compare = CompareRow(
            **detail.model_dump(),
            xpts=score_data["xpts"],
            ppg=float(row.get("points_per_game", 0) or 0),
        )
        results.append(compare.model_dump(exclude_none=True))

    return results


@mcp.tool()
async def get_enriched(
    player_id: Annotated[int | None, "Player ID"] = None,
    name: Annotated[str | None, "Player name"] = None,
) -> dict:
    """Get advanced stats from Understat (xG, xA, shots) and FBref (SOT, progressive passes)."""
    from fpl_oracle.enrichment import understat, fbref

    # Resolve player name
    if player_id:
        row = await db.fetch_one(f"{_PLAYER_JOIN} WHERE p.id = $1", player_id)
    elif name:
        row = await db.fetch_one(
            f"{_PLAYER_JOIN} WHERE LOWER(p.web_name) = $1 LIMIT 1",
            name.lower(),
        )
    else:
        return {"error": "Provide player_id or name"}

    if not row:
        return {"error": "Player not found"}

    full_name = f"{row.get('first_name', '')} {row.get('second_name', '')}".strip()
    web_name = row["web_name"]

    # Try full name first, then web_name
    us_data = await understat.enrich(full_name)
    if not us_data:
        us_data = await understat.enrich(web_name)

    fb_data = await fbref.enrich(full_name)
    if not fb_data:
        fb_data = await fbref.enrich(web_name)

    summary_parts = []
    if us_data:
        summary_parts.append("Understat: xG analysis available")
    if fb_data:
        summary_parts.append("FBref: shooting/passing data available")
    if not us_data and not fb_data:
        summary_parts.append("No external data found")

    result = EnrichedStats(
        player=_player_ref(row),
        understat=us_data,
        fbref=fb_data,
        summary="; ".join(summary_parts),
    )
    return result.model_dump(exclude_none=True)

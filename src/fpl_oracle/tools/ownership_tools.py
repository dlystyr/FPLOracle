"""Ownership tools: template players, EO analysis, price predictions."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.analytics.ownership import template_players, effective_ownership, differential_impact
from fpl_oracle.models import POS_MAP, POS_REVERSE
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def ownership_analysis(
    mode: Annotated[str, "'template' (must-have players >20%), 'eo' (effective ownership for a player), or 'differential' (low-EO high-xP picks)"] = "template",
    player_id: Annotated[int | None, "Player ID for 'eo' mode"] = None,
    position: Annotated[str | None, "Filter: GK, DEF, MID, FWD"] = None,
    limit: Annotated[int, "Max results (default 15)"] = 15,
) -> dict | list[dict]:
    """Ownership intelligence. Template = must-have players (missing their hauls damages rank). EO = effective ownership including captaincy. Differential = low-owned high-xP players for rank gains."""
    if mode == "template":
        pos_int = POS_REVERSE.get(position.upper()) if position else None
        return {"template_players": await template_players(20.0, pos_int, limit)}

    elif mode == "eo":
        if not player_id:
            return {"error": "Provide player_id for EO analysis"}
        return await effective_ownership(player_id)

    elif mode == "differential":
        # Low ownership + good form + decent xPts
        conditions = [
            "p.selected_by_percent <= 10",
            "p.form >= 4",
            "p.minutes >= 270",
            "p.status = 'a'",
        ]
        params: list = []
        idx = 1

        if position:
            et = POS_REVERSE.get(position.upper())
            if et:
                conditions.append(f"p.element_type = ${idx}")
                params.append(et)
                idx += 1

        rows = await db.fetch_all(
            f"SELECT p.id, p.web_name, t.short_name, p.element_type, "
            f"p.now_cost, p.selected_by_percent, p.form, p.total_points, "
            f"p.expected_goals, p.expected_assists, p.minutes "
            f"FROM players p JOIN teams t ON p.team_id = t.id "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY p.form DESC LIMIT ${idx}",
            *params, limit,
        )

        results = []
        for r in rows:
            xgi = float(r.get("expected_goals", 0) or 0) + float(r.get("expected_assists", 0) or 0)
            ownership = float(r["selected_by_percent"])
            # Differential score: form * (100 - ownership) / 100
            diff_score = float(r.get("form", 0) or 0) * (100 - ownership) / 100

            results.append({
                "id": r["id"],
                "name": r["web_name"],
                "team": r["short_name"],
                "pos": POS_MAP.get(r["element_type"], "???"),
                "price": round(r["now_cost"] / 10, 1),
                "ownership": ownership,
                "form": float(r.get("form", 0) or 0),
                "xgi": round(xgi, 2),
                "differential_score": round(diff_score, 2),
                "rank_impact": "high" if diff_score > 6 else "medium" if diff_score > 3 else "low",
            })

        results.sort(key=lambda x: x["differential_score"], reverse=True)
        return {"differential_picks": results}

    return {"error": "mode must be 'template', 'eo', or 'differential'"}


@mcp.tool()
async def price_predictions(
    show: Annotated[str, "'risers', 'fallers', or 'both' (default)"] = "both",
    limit: Annotated[int, "Max results per category (default 10)"] = 10,
) -> dict:
    """Price change predictions based on transfer activity. Risers = buy before price increase to capture value. Fallers = sell before price drops."""
    result: dict = {}

    if show in ("risers", "both"):
        risers = await db.fetch_all(
            "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost, "
            "p.transfers_in_event, p.transfers_out_event, "
            "(p.transfers_in_event - p.transfers_out_event) AS net_transfers, "
            "p.selected_by_percent, p.form "
            "FROM players p JOIN teams t ON p.team_id = t.id "
            "WHERE (p.transfers_in_event - p.transfers_out_event) > 20000 "
            "ORDER BY net_transfers DESC LIMIT $1",
            limit,
        )
        result["likely_risers"] = [
            {
                "id": r["id"],
                "name": r["web_name"],
                "team": r["short_name"],
                "pos": POS_MAP.get(r["element_type"], "???"),
                "price": round(r["now_cost"] / 10, 1),
                "net_transfers": r["net_transfers"],
                "ownership": float(r["selected_by_percent"]),
                "form": float(r.get("form", 0) or 0),
                "confidence": (
                    "high" if r["net_transfers"] > 100000 else
                    "medium" if r["net_transfers"] > 50000 else "low"
                ),
            }
            for r in risers
        ]

    if show in ("fallers", "both"):
        fallers = await db.fetch_all(
            "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost, "
            "p.transfers_in_event, p.transfers_out_event, "
            "(p.transfers_in_event - p.transfers_out_event) AS net_transfers, "
            "p.selected_by_percent, p.form "
            "FROM players p JOIN teams t ON p.team_id = t.id "
            "WHERE (p.transfers_in_event - p.transfers_out_event) < -20000 "
            "ORDER BY net_transfers ASC LIMIT $1",
            limit,
        )
        result["likely_fallers"] = [
            {
                "id": r["id"],
                "name": r["web_name"],
                "team": r["short_name"],
                "pos": POS_MAP.get(r["element_type"], "???"),
                "price": round(r["now_cost"] / 10, 1),
                "net_transfers": r["net_transfers"],
                "ownership": float(r["selected_by_percent"]),
                "confidence": (
                    "high" if r["net_transfers"] < -100000 else
                    "medium" if r["net_transfers"] < -50000 else "low"
                ),
            }
            for r in fallers
        ]

    return result

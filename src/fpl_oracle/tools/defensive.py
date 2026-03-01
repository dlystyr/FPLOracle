"""Defensive contributions tool: CBIT tracking for 2025/26 bonus points."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db
from fpl_oracle.models import POS_MAP, POS_REVERSE
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def defensive_value(
    position: Annotated[str | None, "Filter: DEF or MID (default DEF)"] = "DEF",
    limit: Annotated[int, "Max results (default 15)"] = 15,
) -> list[dict]:
    """Find defenders/midfielders with high defensive contribution (CBIT). In 2025/26, DEFs earn 2 bonus pts for 10+ combined clearances/blocks/interceptions/tackles per match. MIDs/FWDs need 12+ CBIRT."""
    from fpl_oracle.enrichment import fbref

    pos = position.upper() if position else "DEF"
    threshold = 10 if pos == "DEF" else 12

    # Get FPL data for the position
    et = POS_REVERSE.get(pos, 2)
    rows = await db.fetch_all(
        "SELECT p.id, p.web_name, p.first_name, p.second_name, "
        "t.short_name, p.element_type, p.now_cost, p.clean_sheets, "
        "p.total_points, p.form, p.minutes, p.selected_by_percent "
        "FROM players p JOIN teams t ON p.team_id = t.id "
        "WHERE p.element_type = $1 AND p.status = 'a' AND p.minutes >= 450 "
        "ORDER BY p.total_points DESC LIMIT $2",
        et, limit * 2,
    )

    results = []
    for r in rows:
        full_name = f"{r.get('first_name', '')} {r.get('second_name', '')}".strip()
        web_name = r["web_name"]

        # Try to get CBIT from FBref enrichment
        fb_data = await fbref.enrich(full_name)
        if not fb_data:
            fb_data = await fbref.enrich(web_name)

        cbit_per_game = 0.0
        cbit_detail = {}
        if fb_data:
            cbit_per_game = fb_data.get("cbit_per_game", 0)
            cbit_detail = {
                "tackles": fb_data.get("tackles", 0),
                "interceptions": fb_data.get("interceptions", 0),
                "blocks": fb_data.get("blocks", 0),
                "clearances": fb_data.get("clearances", 0),
                "cbit_total": fb_data.get("cbit_total", 0),
                "cbit_per_game": cbit_per_game,
                "pressures": fb_data.get("pressures", 0),
            }

        hits_threshold = cbit_per_game >= threshold
        # Estimate bonus pts from CBIT per season
        games_played = r["minutes"] / 90 if r["minutes"] else 0
        est_cbit_bonus_pts = round(games_played * (0.7 if hits_threshold else 0.2) * 2, 1)

        results.append({
            "id": r["id"],
            "name": r["web_name"],
            "team": r["short_name"],
            "pos": POS_MAP.get(r["element_type"], "???"),
            "price": round(r["now_cost"] / 10, 1),
            "clean_sheets": r["clean_sheets"],
            "form": float(r.get("form", 0) or 0),
            "ownership": float(r.get("selected_by_percent", 0) or 0),
            "cbit": cbit_detail if cbit_detail else "enrichment unavailable",
            "hits_threshold": hits_threshold,
            "threshold": threshold,
            "est_cbit_bonus_season": est_cbit_bonus_pts,
            "verdict": (
                f"Averaging {cbit_per_game:.1f} CBIT/game — "
                + ("regularly hits bonus threshold" if hits_threshold
                   else f"below {threshold} threshold")
            ) if cbit_per_game > 0 else "No CBIT data available",
        })

    # Sort by CBIT per game
    results.sort(
        key=lambda x: x.get("cbit", {}).get("cbit_per_game", 0)
        if isinstance(x.get("cbit"), dict) else 0,
        reverse=True,
    )
    return results[:limit]

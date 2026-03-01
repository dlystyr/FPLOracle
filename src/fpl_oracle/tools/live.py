"""Live gameweek tools: BPS, deadline, set piece takers, price changes."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db, fpl_api
from fpl_oracle.models import POS_MAP, PlayerRef
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def gameweek_live(
    section: Annotated[
        str,
        "What to show: 'bps' (bonus point standings), 'deadline' (next deadline), "
        "'set_pieces' (set piece takers), 'price_changes' (predicted price movers), "
        "or 'all' for everything",
    ] = "all",
) -> dict:
    """Live gameweek hub: BPS standings, deadline, set piece takers, price change predictions."""
    result: dict = {}

    current_gw_row = await db.fetch_one(
        "SELECT id, deadline_time, is_current FROM events "
        "WHERE is_current = TRUE OR is_next = TRUE ORDER BY id LIMIT 1"
    )
    current_gw = current_gw_row["id"] if current_gw_row else 1

    if section in ("deadline", "all"):
        next_event = await db.fetch_one(
            "SELECT id, name, deadline_time FROM events "
            "WHERE is_next = TRUE LIMIT 1"
        )
        if next_event:
            result["deadline"] = {
                "gameweek": next_event["id"],
                "name": next_event["name"],
                "deadline": next_event["deadline_time"].isoformat()
                if next_event["deadline_time"]
                else None,
            }

    if section in ("bps", "all"):
        # Top BPS from current/active GW
        try:
            live_data = await fpl_api.event_live(current_gw)
            elements = live_data.get("elements", [])
            # Sort by bps descending
            bps_leaders = sorted(
                [e for e in elements if e.get("stats", {}).get("bps", 0) > 0],
                key=lambda x: x.get("stats", {}).get("bps", 0),
                reverse=True,
            )[:15]

            bps_result = []
            for e in bps_leaders:
                pid = e["id"]
                row = await db.fetch_one(
                    "SELECT p.web_name, t.short_name, p.element_type, p.now_cost "
                    "FROM players p JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
                    pid,
                )
                if row:
                    stats = e.get("stats", {})
                    bps_result.append({
                        "player": row["web_name"],
                        "team": row["short_name"],
                        "bps": stats.get("bps", 0),
                        "points": stats.get("total_points", 0),
                        "goals": stats.get("goals_scored", 0),
                        "assists": stats.get("assists", 0),
                        "bonus": stats.get("bonus", 0),
                    })
            result["bps_leaders"] = bps_result
        except Exception:
            result["bps_leaders"] = []
            log.warning("bps_fetch_failed", exc_info=True)

    if section in ("set_pieces", "all"):
        # Players with high threat + creativity (proxy for set piece duties)
        sp_rows = await db.fetch_all(
            "SELECT p.id, p.web_name, t.short_name, p.element_type, p.now_cost, "
            "p.creativity, p.threat, p.penalties_saved + p.penalties_missed AS pen_involvement "
            "FROM players p JOIN teams t ON p.team_id = t.id "
            "WHERE p.status = 'a' AND p.minutes >= 450 "
            "ORDER BY (p.creativity + p.threat) DESC LIMIT 20"
        )
        # Group by team for set piece identification
        by_team: dict[str, list] = {}
        for r in sp_rows:
            team = r["short_name"]
            by_team.setdefault(team, []).append({
                "name": r["web_name"],
                "creativity": round(float(r["creativity"]), 1),
                "threat": round(float(r["threat"]), 1),
            })

        result["set_piece_indicators"] = {
            team: players[:2] for team, players in by_team.items()
        }

    if section in ("price_changes", "all"):
        # From v_price_change_candidates view
        changes = await db.fetch_all(
            "SELECT * FROM v_price_change_candidates "
            "WHERE prediction != 'stable' "
            "ORDER BY ABS(net_transfers) DESC LIMIT 15"
        )
        result["price_changes"] = [
            {
                "player": r["web_name"],
                "team": r["team"],
                "price": round(r["now_cost"] / 10, 1),
                "net_transfers": r["net_transfers"],
                "prediction": r["prediction"],
            }
            for r in changes
        ]

    return result

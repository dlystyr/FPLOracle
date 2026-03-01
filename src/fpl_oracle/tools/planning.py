"""Planning tools: evaluate_hit, transfer_planner, chip_planner."""

from __future__ import annotations

from typing import Annotated

from fpl_oracle.server import mcp
from fpl_oracle import db, fpl_api
from fpl_oracle.analytics import hits as hit_analytics
from fpl_oracle.analytics import chips as chip_analytics
from fpl_oracle.analytics import xpts, scoring
from fpl_oracle.models import POS_MAP
from fpl_oracle.log import get_logger

log = get_logger(__name__)


@mcp.tool()
async def evaluate_hit(
    player_out_id: Annotated[int, "Player ID to transfer out"],
    player_in_id: Annotated[int, "Player ID to transfer in"],
    horizon: Annotated[int, "GWs to evaluate over (default 5)"] = 5,
) -> dict:
    """Evaluate if a -4 hit transfer is worth it. Compares xPts differential over the horizon minus the 4-point cost."""
    return await hit_analytics.evaluate_hit(player_out_id, player_in_id, horizon)


@mcp.tool()
async def chip_planner(
    chip: Annotated[str | None, "Specific chip: 'triple_captain', 'bench_boost', 'free_hit', 'wildcard', or omit for full calendar"] = None,
) -> dict:
    """Chip timing optimizer. Finds the best gameweek for each chip based on DGW/BGW patterns and fixture swings."""
    if chip == "triple_captain":
        return {"triple_captain": await chip_analytics.analyze_triple_captain()}
    elif chip == "bench_boost":
        return {"bench_boost": await chip_analytics.analyze_bench_boost()}
    elif chip == "free_hit":
        return {"free_hit": await chip_analytics.analyze_free_hit()}
    elif chip == "wildcard":
        return {"wildcard": await chip_analytics.analyze_wildcard()}
    else:
        return await chip_analytics.chip_calendar()


@mcp.tool()
async def transfer_planner(
    manager_id: Annotated[int, "FPL manager ID"],
    num_weeks: Annotated[int, "Planning horizon in GWs (default 5)"] = 5,
    free_transfers: Annotated[int, "Current free transfers (default 1)"] = 1,
) -> dict:
    """Multi-GW transfer plan. Plans transfers across multiple gameweeks, accounting for free transfer value, -4 hits, and fixture swings."""
    current_gw_row = await db.fetch_one(
        "SELECT id FROM events WHERE is_current = TRUE LIMIT 1"
    )
    current_gw = current_gw_row["id"] if current_gw_row else 1

    # Fetch squad
    try:
        picks_data = await fpl_api.manager_picks(manager_id, current_gw)
    except Exception:
        return {"error": "Could not fetch team picks"}

    if not picks_data or "picks" not in picks_data:
        return {"error": "No picks data"}

    squad_ids = [p["element"] for p in picks_data["picks"]]
    team_strengths = await xpts.calculate_team_strength()

    # Score each squad player
    squad_scores: list[dict] = []
    for pid in squad_ids:
        row = await db.fetch_one(
            "SELECT p.*, t.short_name FROM players p "
            "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
            pid,
        )
        if not row:
            continue

        fix = await db.fetch_all(
            "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
            "AND NOT finished ORDER BY event LIMIT $2",
            row["team_id"], num_weeks,
        )
        score_data = await scoring.score_player(
            row, fix, team_strengths, current_gw
        )

        # Weakness indicators
        weakness = 0
        reasons = []
        if row.get("status") != "a":
            weakness += 40
            reasons.append(f"unavailable ({row.get('status')})")
        if float(row.get("form", 0) or 0) < 2.0:
            weakness += 20
            reasons.append("very poor form")
        elif float(row.get("form", 0) or 0) < 3.5:
            weakness += 10
            reasons.append("below-average form")
        if score_data["xpts"] < 8:
            weakness += 15
            reasons.append(f"low xPts ({score_data['xpts']})")
        if "tough" in (score_data.get("fixture_run") or ""):
            weakness += 10
            reasons.append("tough fixtures ahead")

        squad_scores.append({
            "id": row["id"],
            "name": row["web_name"],
            "team": row["short_name"],
            "pos": POS_MAP.get(row["element_type"], "???"),
            "price": round(row["now_cost"] / 10, 1),
            "element_type": row["element_type"],
            "now_cost": row["now_cost"],
            "xpts": score_data["xpts"],
            "fixture_run": score_data.get("fixture_run"),
            "weakness": weakness,
            "reasons": reasons,
        })

    # Sort by weakness (worst first)
    squad_scores.sort(key=lambda x: x["weakness"], reverse=True)

    # Plan transfers
    plan: list[dict] = []
    ft_remaining = free_transfers
    total_hits = 0

    for gw_offset in range(num_weeks):
        gw = current_gw + gw_offset
        if gw_offset > 0:
            ft_remaining = min(ft_remaining + 1, 5)  # Max 5 FTs

        # Find weakest player not yet replaced
        replaced_ids = {t["out_id"] for t in plan}
        candidates = [s for s in squad_scores if s["weakness"] >= 15 and s["id"] not in replaced_ids]

        if not candidates:
            continue

        target = candidates[0]

        # Find best replacement
        replacement = await db.fetch_all(
            "SELECT p.*, t.short_name FROM players p "
            "JOIN teams t ON p.team_id = t.id "
            "WHERE p.element_type = $1 AND p.now_cost <= $2 "
            "AND p.status = 'a' AND p.minutes >= 90 "
            "AND p.id != ALL($3::int[]) "
            "ORDER BY p.form DESC LIMIT 5",
            target["element_type"],
            target["now_cost"] + 5,
            squad_ids,
        )

        best_repl = None
        best_xpts = 0.0
        for r in replacement:
            r_fix = await db.fetch_all(
                "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
                "AND NOT finished ORDER BY event LIMIT $2",
                r["team_id"], num_weeks - gw_offset,
            )
            r_score = await scoring.score_player(
                r, r_fix, team_strengths, current_gw
            )
            if r_score["xpts"] > best_xpts:
                best_xpts = r_score["xpts"]
                best_repl = (r, r_score)

        if not best_repl:
            continue

        r, r_score = best_repl
        is_hit = ft_remaining <= 0
        if is_hit:
            # Only take hit if net gain > 4
            net = best_xpts - target["xpts"] - 4
            if net <= 0:
                continue
            total_hits += 1
        else:
            ft_remaining -= 1

        plan.append({
            "gameweek": gw,
            "out_id": target["id"],
            "out": f"{target['name']} ({target['team']})",
            "out_xpts": target["xpts"],
            "out_reasons": target["reasons"],
            "in_id": r["id"],
            "in": f"{r['web_name']} ({r['short_name']})",
            "in_xpts": round(best_xpts, 2),
            "net_cost": round((r["now_cost"] - target["now_cost"]) / 10, 1),
            "is_hit": is_hit,
            "xpts_gain": round(best_xpts - target["xpts"] - (4 if is_hit else 0), 2),
        })

    return {
        "horizon": num_weeks,
        "transfers_planned": len(plan),
        "hits_taken": total_hits,
        "total_hit_cost": total_hits * 4,
        "total_xpts_gain": round(sum(t["xpts_gain"] for t in plan), 2),
        "plan": plan,
        "advice": (
            f"Plan {len(plan)} transfers over {num_weeks} GWs"
            + (f" including {total_hits} hit(s)" if total_hits else "")
            + f" for +{round(sum(t['xpts_gain'] for t in plan), 1)} net xPts."
        ),
    }

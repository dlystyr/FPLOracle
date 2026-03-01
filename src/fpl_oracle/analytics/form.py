"""Form analysis: trajectory, momentum, streaks, ICT trends."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.log import get_logger

log = get_logger(__name__)


async def form_trajectory(player_id: int) -> dict[str, Any]:
    """Analyse a player's form trend over recent gameweeks.

    Returns momentum classification (rising/stable/falling) and ICT trend.
    """
    snapshots = await db.fetch_all(
        "SELECT gameweek, form, total_points, ict_index, minutes "
        "FROM player_snapshots WHERE player_id = $1 "
        "ORDER BY gameweek DESC LIMIT 6",
        player_id,
    )
    if len(snapshots) < 2:
        return {"momentum": "unknown", "ict_trend": "unknown", "snapshots": []}

    # Recent 3 GW avg vs older 3 GW avg
    recent = snapshots[:3]
    older = snapshots[3:6]

    recent_avg = sum(float(s.get("form", 0) or 0) for s in recent) / len(recent)
    if older:
        older_avg = sum(float(s.get("form", 0) or 0) for s in older) / len(older)
    else:
        older_avg = recent_avg

    momentum_score = recent_avg - older_avg
    if momentum_score > 1.0:
        momentum = "rising"
    elif momentum_score < -1.0:
        momentum = "falling"
    else:
        momentum = "stable"

    # ICT trend
    recent_ict = sum(float(s.get("ict_index", 0) or 0) for s in recent) / len(recent)
    if older:
        older_ict = sum(float(s.get("ict_index", 0) or 0) for s in older) / len(older)
    else:
        older_ict = recent_ict

    ict_diff = recent_ict - older_ict
    if ict_diff > 5:
        ict_trend = "improving"
    elif ict_diff < -5:
        ict_trend = "declining"
    else:
        ict_trend = "stable"

    return {
        "momentum": momentum,
        "momentum_score": round(momentum_score, 2),
        "ict_trend": ict_trend,
        "recent_form_avg": round(recent_avg, 2),
        "snapshots": [
            {
                "gw": s["gameweek"],
                "form": float(s.get("form", 0) or 0),
                "pts": s.get("total_points", 0),
                "ict": float(s.get("ict_index", 0) or 0),
            }
            for s in recent
        ],
    }


async def team_form(team_id: int) -> dict[str, Any]:
    """Last 5 results with W/D/L record, goals, clean sheets."""
    results = await db.fetch_all(
        "SELECT tr.result, tr.goals_for, tr.goals_against, tr.clean_sheet, "
        "tr.was_home, t2.short_name AS opponent "
        "FROM team_results tr "
        "JOIN teams t2 ON tr.opponent_id = t2.id "
        "WHERE tr.team_id = $1 ORDER BY tr.event DESC LIMIT 5",
        team_id,
    )
    if not results:
        return {"form": "N/A", "record": {}, "results": []}

    record = {"W": 0, "D": 0, "L": 0}
    gf = ga = cs = 0
    for r in results:
        record[r["result"]] = record.get(r["result"], 0) + 1
        gf += r["goals_for"] or 0
        ga += r["goals_against"] or 0
        if r["clean_sheet"]:
            cs += 1

    form_str = "".join(r["result"] for r in results)

    return {
        "form": form_str,
        "record": record,
        "goals_for": gf,
        "goals_against": ga,
        "clean_sheets": cs,
        "results": [
            {
                "vs": r["opponent"],
                "home": r["was_home"],
                "result": r["result"],
                "score": f"{r['goals_for']}-{r['goals_against']}",
            }
            for r in results
        ],
    }


def nailed_score(player: dict[str, Any], recent_history: list[dict[str, Any]]) -> int:
    """Calculate how 'nailed' a player is (0-100).

    Uses minutes per game, start rate, status, and recent benching.
    """
    minutes = player.get("minutes", 0) or 0
    starts = player.get("starts", 0) or 0
    status = player.get("status", "a")
    chance = player.get("chance_of_playing_next_round")

    finished = max(len(recent_history), 1)
    mpg = minutes / finished

    if mpg >= 85:
        score = 95
    elif mpg >= 75:
        score = 85
    elif mpg >= 60:
        score = 70
    elif mpg >= 45:
        score = 50
    else:
        score = 30

    # Start rate bonus/penalty
    total_appearances = starts + sum(
        1 for h in recent_history if (h.get("minutes", 0) or 0) > 0
    )
    if total_appearances > 0:
        start_rate = starts / total_appearances * 100
        if start_rate >= 90:
            score += 5
        elif start_rate < 70:
            score -= 10

    # Status penalty
    if status != "a":
        score -= 30

    # Chance of playing
    if chance is not None and chance < 100:
        score -= (100 - chance) // 2

    # Recent sub/bench appearances
    last_3 = recent_history[:3]
    sub_count = sum(1 for h in last_3 if 0 < (h.get("minutes", 0) or 0) < 60)
    if sub_count >= 2:
        score -= 10

    # Benched last game
    if recent_history and (recent_history[0].get("minutes", 0) or 0) == 0:
        score -= 15

    return max(0, min(100, score))

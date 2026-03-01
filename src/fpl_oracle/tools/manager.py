"""Manager tools: my_team analysis, transfer suggestions."""

from __future__ import annotations

from typing import Annotated, Any

from fpl_oracle.server import mcp
from fpl_oracle import db, fpl_api
from fpl_oracle.models import (
    POS_MAP,
    ManagerOverview,
    SquadPlayer,
    TransferSuggestion,
    PlayerRef,
    ScoredPlayer,
)
from fpl_oracle.analytics import xpts, scoring, form
from fpl_oracle.log import get_logger

log = get_logger(__name__)


async def _get_current_gw() -> int:
    row = await db.fetch_one("SELECT id FROM events WHERE is_current = TRUE LIMIT 1")
    return row["id"] if row else 1


def _player_ref_from_row(row: dict) -> PlayerRef:
    return PlayerRef(
        id=row["id"],
        name=row["web_name"],
        team=row["short_name"],
        pos=POS_MAP.get(row["element_type"], "???"),
        price=round(row["now_cost"] / 10, 1),
    )


@mcp.tool()
async def my_team(
    manager_id: Annotated[int, "FPL manager ID (find at fpl website URL)"],
) -> dict:
    """Full analysis of an FPL team: squad, xPts, weaknesses, captain picks. Provide your FPL manager ID."""
    # Fetch manager info
    try:
        info = await fpl_api.manager_info(manager_id)
    except Exception:
        return {"error": f"Could not fetch manager {manager_id}"}

    current_gw = await _get_current_gw()

    overview = ManagerOverview(
        manager_id=manager_id,
        name=f"{info.get('player_first_name', '')} {info.get('player_last_name', '')}".strip(),
        team_name=info.get("name", ""),
        overall_rank=info.get("summary_overall_rank"),
        overall_points=info.get("summary_overall_points"),
        bank=round((info.get("last_deadline_bank", 0) or 0) / 10, 1),
        team_value=round((info.get("last_deadline_value", 0) or 0) / 10, 1),
    )

    # Fetch picks
    try:
        picks_data = await fpl_api.manager_picks(manager_id, current_gw)
    except Exception:
        picks_data = None

    if not picks_data or "picks" not in picks_data:
        return {
            "overview": overview.model_dump(exclude_none=True),
            "error": "Could not fetch picks for current GW",
        }

    picks = picks_data["picks"]
    team_strengths = await xpts.calculate_team_strength()

    squad: list[dict] = []
    weaknesses: list[dict] = []

    for pick in picks:
        pid = pick["element"]
        row = await db.fetch_one(
            "SELECT p.*, t.short_name FROM players p "
            "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
            pid,
        )
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

        sp = SquadPlayer(
            id=row["id"],
            name=row["web_name"],
            team=row["short_name"],
            pos=POS_MAP.get(row["element_type"], "???"),
            price=round(row["now_cost"] / 10, 1),
            xpts=score_data["xpts"],
            form=float(row.get("form", 0) or 0),
            minutes=row.get("minutes", 0) or 0,
            fixture_run=score_data.get("fixture_run"),
            score=score_data["composite"],
            starting=pick.get("multiplier", 0) > 0,
            captain=pick.get("is_captain", False),
            vice_captain=pick.get("is_vice_captain", False),
            multiplier=pick.get("multiplier", 1),
        )
        squad.append(sp.model_dump(exclude_none=True))

        # Identify weaknesses
        weakness_score = 0
        reasons = []
        if row.get("status") != "a":
            weakness_score += 30
            reasons.append(f"status: {row.get('status')}")
        if float(row.get("form", 0) or 0) < 3.0:
            weakness_score += 15
            reasons.append("poor form")
        if score_data.get("fixture_run", "").endswith("tough)"):
            weakness_score += 10
            reasons.append("tough fixtures")

        if weakness_score >= 15:
            weaknesses.append({
                "player": _player_ref_from_row(row).model_dump(),
                "weakness_score": weakness_score,
                "reasons": reasons,
            })

    weaknesses.sort(key=lambda x: x["weakness_score"], reverse=True)

    return {
        "overview": overview.model_dump(exclude_none=True),
        "squad": squad,
        "weaknesses": weaknesses[:5],
    }


@mcp.tool()
async def transfer_suggestions(
    manager_id: Annotated[int, "FPL manager ID"],
    position: Annotated[str | None, "Filter replacements: GK, DEF, MID, FWD"] = None,
    limit: Annotated[int, "Max suggestions (default 5)"] = 5,
) -> list[dict]:
    """Get transfer-in recommendations based on your team's weak spots."""
    current_gw = await _get_current_gw()

    # Fetch current squad
    try:
        picks_data = await fpl_api.manager_picks(manager_id, current_gw)
    except Exception:
        return [{"error": "Could not fetch team picks"}]

    if not picks_data or "picks" not in picks_data:
        return [{"error": "No picks data"}]

    squad_ids = [p["element"] for p in picks_data["picks"]]
    team_strengths = await xpts.calculate_team_strength()

    # Find weakest players in squad
    weak_players: list[dict[str, Any]] = []
    for pid in squad_ids:
        row = await db.fetch_one(
            "SELECT p.*, t.short_name FROM players p "
            "JOIN teams t ON p.team_id = t.id WHERE p.id = $1",
            pid,
        )
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

        weakness = 0
        if row.get("status") != "a":
            weakness += 30
        if float(row.get("form", 0) or 0) < 3.0:
            weakness += 15
        if score_data["xpts"] < 10:
            weakness += 10

        if weakness > 0:
            weak_players.append({**row, "_weakness": weakness, "_score": score_data})

    weak_players.sort(key=lambda x: x["_weakness"], reverse=True)

    suggestions: list[dict] = []
    for weak in weak_players[:limit]:
        elem_type = weak["element_type"]
        price_cap = weak["now_cost"] + 10  # allow 0.1m over

        # Find replacement
        from fpl_oracle.models import POS_REVERSE

        conditions = [
            "p.element_type = $1",
            "p.now_cost <= $2",
            "p.status = 'a'",
            "p.minutes >= 90",
            "p.id != ALL($3::int[])",
        ]
        repl_rows = await db.fetch_all(
            f"SELECT p.*, t.short_name FROM players p "
            f"JOIN teams t ON p.team_id = t.id "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY p.form DESC LIMIT 10",
            elem_type, price_cap, squad_ids,
        )

        # Score replacements
        best = None
        best_xpts = 0.0
        for r in repl_rows:
            r_fix = await db.fetch_all(
                "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
                "AND NOT finished ORDER BY event LIMIT 5",
                r["team_id"],
            )
            r_score = await scoring.score_player(
                r, r_fix, team_strengths, current_gw
            )
            if r_score["xpts"] > best_xpts:
                best_xpts = r_score["xpts"]
                best = (r, r_score)

        if best:
            r, r_score = best
            out_ref = _player_ref_from_row(weak)
            in_ref = ScoredPlayer(
                id=r["id"],
                name=r["web_name"],
                team=r["short_name"],
                pos=POS_MAP.get(r["element_type"], "???"),
                price=round(r["now_cost"] / 10, 1),
                xpts=r_score["xpts"],
                form=float(r.get("form", 0) or 0),
                minutes=r.get("minutes", 0) or 0,
                fixture_run=r_score.get("fixture_run"),
                score=r_score["composite"],
            )
            weak_xpts = weak["_score"]["xpts"]

            reasons = []
            if weak.get("status") != "a":
                reasons.append(f"out is {weak.get('status')}")
            if float(weak.get("form", 0) or 0) < 3.0:
                reasons.append("poor form")
            reasons.append(f"+{round(best_xpts - weak_xpts, 1)} xPts")

            suggestion = TransferSuggestion(
                out=out_ref,
                in_=in_ref,
                net_cost=round((r["now_cost"] - weak["now_cost"]) / 10, 1),
                xpts_gain=round(best_xpts - weak_xpts, 2),
                reason="; ".join(reasons),
            )
            suggestions.append(suggestion.model_dump(exclude_none=True))

    return suggestions

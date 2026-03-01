"""Squad optimizer: PuLP linear programming for wildcard/free hit squads."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.analytics import xpts, scoring
from fpl_oracle.log import get_logger

log = get_logger(__name__)

# Position constraints: element_type -> (min, max) in a 15-man squad
_POS_LIMITS = {1: (2, 2), 2: (5, 5), 3: (5, 5), 4: (3, 3)}


async def build_squad(
    budget: float = 100.0,
    strategy: str = "balanced",
    must_include: list[int] | None = None,
    exclude: list[int] | None = None,
    gameweek: int | None = None,
) -> dict[str, Any]:
    """Build optimal 15-player squad using linear programming.

    strategy: 'balanced', 'attacking' (weight FWD/MID xPts higher),
              'defensive' (weight GK/DEF xPts higher)
    gameweek: if set, optimize for a single GW (free hit mode)
    """
    team_strengths = await xpts.calculate_team_strength()
    current_gw_row = await db.fetch_one(
        "SELECT id FROM events WHERE is_current = TRUE LIMIT 1"
    )
    current_gw = gameweek or (current_gw_row["id"] if current_gw_row else 1)
    horizon = 1 if gameweek else 5

    # Fetch all available players
    rows = await db.fetch_all(
        "SELECT p.*, t.short_name FROM players p "
        "JOIN teams t ON p.team_id = t.id "
        "WHERE p.status = 'a' AND p.minutes >= 90 "
        "ORDER BY p.total_points DESC LIMIT 300"
    )

    # Score each player
    players: list[dict[str, Any]] = []
    for row in rows:
        if exclude and row["id"] in exclude:
            continue
        fixtures = await db.fetch_all(
            "SELECT * FROM fixtures WHERE (team_h = $1 OR team_a = $1) "
            "AND NOT finished ORDER BY event LIMIT $2",
            row["team_id"], horizon,
        )
        xp_data = await xpts.calculate_expected_points(
            row, fixtures, team_strengths, current_gw
        )
        players.append({
            "id": row["id"],
            "name": row["web_name"],
            "team": row["short_name"],
            "team_id": row["team_id"],
            "pos": row["element_type"],
            "cost": row["now_cost"],
            "xpts": xp_data["total"],
            "form": float(row.get("form", 0) or 0),
            "price": round(row["now_cost"] / 10, 1),
        })

    if not players:
        return {"error": "No eligible players found"}

    # Strategy weights
    weights = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
    if strategy == "attacking":
        weights = {1: 0.8, 2: 0.9, 3: 1.1, 4: 1.2}
    elif strategy == "defensive":
        weights = {1: 1.2, 2: 1.1, 3: 1.0, 4: 0.9}

    # Try LP solver
    try:
        squad = _solve_lp(players, budget, weights, must_include or [])
    except Exception:
        log.warning("lp_solver_failed_using_greedy", exc_info=True)
        squad = _greedy_fallback(players, budget, must_include or [])

    if not squad:
        return {"error": "Could not build a valid squad within budget"}

    total_cost = sum(p["cost"] for p in squad) / 10
    total_xpts = sum(p["xpts"] * weights.get(p["pos"], 1.0) for p in squad)

    # Pick starting 11
    starting, bench = _pick_starting_11(squad)

    return {
        "squad": [_format(p, weights) for p in squad],
        "starting_11": [_format(p, weights) for p in starting],
        "bench": [_format(p, weights) for p in bench],
        "total_cost": round(total_cost, 1),
        "budget_remaining": round(budget - total_cost, 1),
        "total_xpts": round(total_xpts, 2),
        "strategy": strategy,
        "horizon": f"{'GW' + str(gameweek) if gameweek else str(horizon) + ' GWs'}",
    }


def _format(p: dict, weights: dict) -> dict:
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    return {
        "id": p["id"],
        "name": p["name"],
        "team": p["team"],
        "pos": pos_map.get(p["pos"], "???"),
        "price": p["price"],
        "xpts": round(p["xpts"] * weights.get(p["pos"], 1.0), 2),
    }


def _solve_lp(
    players: list[dict],
    budget: float,
    weights: dict[int, float],
    must_include: list[int],
) -> list[dict]:
    """Solve squad selection via PuLP linear programming."""
    import pulp

    prob = pulp.LpProblem("FPL_Squad", pulp.LpMaximize)

    # Decision variables: 1 if player selected, 0 otherwise
    selected = {
        p["id"]: pulp.LpVariable(f"x_{p['id']}", cat="Binary")
        for p in players
    }

    # Objective: maximize weighted xPts
    prob += pulp.lpSum(
        selected[p["id"]] * p["xpts"] * weights.get(p["pos"], 1.0)
        for p in players
    )

    # Budget constraint
    prob += (
        pulp.lpSum(selected[p["id"]] * p["cost"] for p in players)
        <= budget * 10
    )

    # Position constraints
    for pos, (lo, hi) in _POS_LIMITS.items():
        pos_players = [p for p in players if p["pos"] == pos]
        prob += pulp.lpSum(selected[p["id"]] for p in pos_players) >= lo
        prob += pulp.lpSum(selected[p["id"]] for p in pos_players) <= hi

    # Max 3 from any single team
    team_ids = set(p["team_id"] for p in players)
    for tid in team_ids:
        team_players = [p for p in players if p["team_id"] == tid]
        prob += pulp.lpSum(selected[p["id"]] for p in team_players) <= 3

    # Total squad = 15
    prob += pulp.lpSum(selected[p["id"]] for p in players) == 15

    # Must-include constraints
    for pid in must_include:
        if pid in selected:
            prob += selected[pid] == 1

    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if prob.status != 1:
        raise ValueError("LP solver did not find optimal solution")

    return [p for p in players if selected[p["id"]].varValue == 1]


def _greedy_fallback(
    players: list[dict],
    budget: float,
    must_include: list[int],
) -> list[dict]:
    """Greedy squad builder as fallback if PuLP fails."""
    budget_remaining = budget * 10
    squad: list[dict] = []
    pos_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    team_counts: dict[int, int] = {}

    # Add must-includes first
    for pid in must_include:
        for p in players:
            if p["id"] == pid:
                squad.append(p)
                pos_counts[p["pos"]] += 1
                team_counts[p["team_id"]] = team_counts.get(p["team_id"], 0) + 1
                budget_remaining -= p["cost"]
                break

    # Sort remaining by xPts descending
    remaining = sorted(
        [p for p in players if p["id"] not in {s["id"] for s in squad}],
        key=lambda x: x["xpts"],
        reverse=True,
    )

    for p in remaining:
        if len(squad) >= 15:
            break
        pos = p["pos"]
        _, max_count = _POS_LIMITS[pos]
        if pos_counts[pos] >= max_count:
            continue
        if team_counts.get(p["team_id"], 0) >= 3:
            continue
        if p["cost"] > budget_remaining:
            continue

        squad.append(p)
        pos_counts[pos] += 1
        team_counts[p["team_id"]] = team_counts.get(p["team_id"], 0) + 1
        budget_remaining -= p["cost"]

    return squad if len(squad) == 15 else []


def _pick_starting_11(squad: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pick optimal starting 11 from 15-man squad.

    Rules: 1 GK, at least 3 DEF, at least 2 MID, at least 1 FWD.
    """
    by_pos: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}
    for p in squad:
        by_pos[p["pos"]].append(p)

    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: x["xpts"], reverse=True)

    starting: list[dict] = []
    bench: list[dict] = []

    # Mandatory: 1 GK
    starting.append(by_pos[1][0])
    bench.append(by_pos[1][1])

    # Minimum: 3 DEF, 2 MID, 1 FWD
    mins = {2: 3, 3: 2, 4: 1}
    for pos, count in mins.items():
        starting.extend(by_pos[pos][:count])

    # Remaining 4 spots: best xPts from leftover outfield
    used_ids = {p["id"] for p in starting}
    leftovers = sorted(
        [p for p in squad if p["id"] not in used_ids and p["pos"] != 1],
        key=lambda x: x["xpts"],
        reverse=True,
    )
    starting.extend(leftovers[:4])
    bench.extend(leftovers[4:])

    return starting, bench

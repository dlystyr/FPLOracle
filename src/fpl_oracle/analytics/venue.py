"""Home/away performance splits."""

from __future__ import annotations

from typing import Any

from fpl_oracle import db
from fpl_oracle.log import get_logger

log = get_logger(__name__)


async def venue_splits(player_id: int) -> dict[str, Any]:
    """Calculate home vs away performance splits for a player."""
    home = await db.fetch_all(
        "SELECT total_points, goals_scored, assists, clean_sheets, "
        "bonus, minutes, expected_goals, expected_assists "
        "FROM player_history WHERE player_id = $1 AND was_home = TRUE",
        player_id,
    )
    away = await db.fetch_all(
        "SELECT total_points, goals_scored, assists, clean_sheets, "
        "bonus, minutes, expected_goals, expected_assists "
        "FROM player_history WHERE player_id = $1 AND was_home = FALSE",
        player_id,
    )

    def _agg(rows: list[dict]) -> dict[str, Any]:
        if not rows:
            return {"games": 0, "ppg": 0, "goals": 0, "assists": 0, "xg": 0, "xa": 0, "cs": 0, "bonus": 0}
        n = len(rows)
        total_pts = sum(r.get("total_points", 0) or 0 for r in rows)
        return {
            "games": n,
            "ppg": round(total_pts / n, 2),
            "total_points": total_pts,
            "goals": sum(r.get("goals_scored", 0) or 0 for r in rows),
            "assists": sum(r.get("assists", 0) or 0 for r in rows),
            "xg": round(sum(float(r.get("expected_goals", 0) or 0) for r in rows), 2),
            "xa": round(sum(float(r.get("expected_assists", 0) or 0) for r in rows), 2),
            "cs": sum(r.get("clean_sheets", 0) or 0 for r in rows),
            "bonus": sum(r.get("bonus", 0) or 0 for r in rows),
        }

    home_stats = _agg(home)
    away_stats = _agg(away)

    # Venue bias
    h_ppg = home_stats["ppg"]
    a_ppg = away_stats["ppg"]
    if h_ppg + a_ppg > 0:
        home_pct = round(h_ppg / (h_ppg + a_ppg) * 100, 1)
    else:
        home_pct = 50.0

    if h_ppg > a_ppg * 1.3:
        bias = "strong_home"
    elif h_ppg > a_ppg * 1.1:
        bias = "slight_home"
    elif a_ppg > h_ppg * 1.3:
        bias = "strong_away"
    elif a_ppg > h_ppg * 1.1:
        bias = "slight_away"
    else:
        bias = "neutral"

    return {
        "home": home_stats,
        "away": away_stats,
        "home_ppg_share": home_pct,
        "venue_bias": bias,
    }

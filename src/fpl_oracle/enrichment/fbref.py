"""FBref data loader with Redis caching.

Enriches with: shooting, passing, GCA/SCA, possession (CPA/PPA),
and defensive actions (for CBIT modeling).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fpl_oracle import cache
from fpl_oracle.enrichment.matcher import match_player, normalize
from fpl_oracle.log import get_logger

log = get_logger(__name__)

_CACHE_KEY = "fpl:enrichment:fbref_epl"
_CACHE_TTL = 86400  # 24 hours
_data: list[dict[str, Any]] | None = None


def _safe_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (ValueError, TypeError):
        return 0


def _safe_float(val: Any) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def _per90(total: float, minutes: int) -> float:
    if minutes < 90:
        return 0.0
    return round(total / minutes * 90, 2)


async def _load_data() -> list[dict[str, Any]]:
    """Load FBref EPL data, with Redis cache."""
    global _data
    if _data:
        return _data

    cached = await cache.get(_CACHE_KEY)
    if cached and isinstance(cached, list):
        _data = cached
        return _data

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_fbref)
        if data:
            _data = data
            await cache.set(_CACHE_KEY, data, ttl=_CACHE_TTL)
            log.info("fbref_loaded", count=len(data))
        return _data or []
    except Exception:
        log.warning("fbref_load_failed", exc_info=True)
        return []


def _merge_by_name(stats: list[dict[str, Any]], name: str, updates: dict[str, Any]) -> None:
    """Merge updates into the stats entry matching name."""
    norm = normalize(name)
    for s in stats:
        if normalize(s["name"]) == norm:
            s.update(updates)
            return


def _fetch_fbref() -> list[dict[str, Any]]:
    import soccerdata

    fbref = soccerdata.FBref(leagues="ENG-Premier League", seasons="2024-2025")
    stats: list[dict[str, Any]] = []

    # --- Standard stats ---
    try:
        standard = fbref.read_player_season_stats(stat_type="standard")
        if standard is not None and not standard.empty:
            for idx, row in standard.iterrows():
                player_name = idx[-1] if isinstance(idx, tuple) else str(idx)
                stats.append({
                    "name": player_name,
                    "minutes": _safe_int(row.get("Min")),
                    "goals": _safe_int(row.get("Gls")),
                    "assists": _safe_int(row.get("Ast")),
                    "xg": _safe_float(row.get("xG")),
                    "xa": _safe_float(row.get("xAG")),
                    "npxg": _safe_float(row.get("npxG")),
                    "g_minus_xg": _safe_float(row.get("G-xG")),
                })
    except Exception:
        log.warning("fbref_standard_failed", exc_info=True)

    # --- Shooting stats ---
    try:
        shooting = fbref.read_player_season_stats(stat_type="shooting")
        if shooting is not None and not shooting.empty:
            for idx, row in shooting.iterrows():
                player_name = idx[-1] if isinstance(idx, tuple) else str(idx)
                _merge_by_name(stats, player_name, {
                    "shots": _safe_int(row.get("Sh")),
                    "shots_on_target": _safe_int(row.get("SoT")),
                    "sot_per_90": _safe_float(row.get("SoT/90")),
                    "sot_pct": _safe_float(row.get("SoT%")),
                    "goals_per_shot": _safe_float(row.get("G/Sh")),
                    "goals_per_sot": _safe_float(row.get("G/SoT")),
                    "avg_shot_distance": _safe_float(row.get("Dist")),
                    "npxg_per_shot": _safe_float(row.get("npxG/Sh")),
                })
    except Exception:
        log.warning("fbref_shooting_failed", exc_info=True)

    # --- Passing stats ---
    try:
        passing = fbref.read_player_season_stats(stat_type="passing")
        if passing is not None and not passing.empty:
            for idx, row in passing.iterrows():
                player_name = idx[-1] if isinstance(idx, tuple) else str(idx)
                mins = 0
                for s in stats:
                    if normalize(s["name"]) == normalize(player_name):
                        mins = s.get("minutes", 0)
                        break
                prog_passes = _safe_int(row.get("PrgP"))
                _merge_by_name(stats, player_name, {
                    "progressive_passes": prog_passes,
                    "progressive_passes_per_90": _per90(prog_passes, mins),
                    "passes_into_final_third": _safe_int(row.get("1/3")),
                    "passes_into_penalty_area": _safe_int(row.get("PPA")),
                    "ppa_per_90": _per90(_safe_int(row.get("PPA")), mins),
                    "crosses": _safe_int(row.get("Crs")),
                })
    except Exception:
        log.warning("fbref_passing_failed", exc_info=True)

    # --- Goal and Shot Creation (GCA/SCA) ---
    try:
        gca = fbref.read_player_season_stats(stat_type="goal_shot_creation")
        if gca is not None and not gca.empty:
            for idx, row in gca.iterrows():
                player_name = idx[-1] if isinstance(idx, tuple) else str(idx)
                mins = 0
                for s in stats:
                    if normalize(s["name"]) == normalize(player_name):
                        mins = s.get("minutes", 0)
                        break
                sca_total = _safe_int(row.get("SCA"))
                gca_total = _safe_int(row.get("GCA"))
                _merge_by_name(stats, player_name, {
                    "sca": sca_total,
                    "sca_per_90": _safe_float(row.get("SCA90")),
                    "gca": gca_total,
                    "gca_per_90": _safe_float(row.get("GCA90")),
                })
    except Exception:
        log.warning("fbref_gca_failed", exc_info=True)

    # --- Possession stats (carries, progressive carries, CPA) ---
    try:
        possession = fbref.read_player_season_stats(stat_type="possession")
        if possession is not None and not possession.empty:
            for idx, row in possession.iterrows():
                player_name = idx[-1] if isinstance(idx, tuple) else str(idx)
                mins = 0
                for s in stats:
                    if normalize(s["name"]) == normalize(player_name):
                        mins = s.get("minutes", 0)
                        break
                prog_carries = _safe_int(row.get("PrgC"))
                cpa = _safe_int(row.get("1/3"))  # carries into final third
                touches_att_pen = _safe_int(row.get("Att Pen"))
                _merge_by_name(stats, player_name, {
                    "touches": _safe_int(row.get("Touches")),
                    "touches_att_pen": touches_att_pen,
                    "touches_att_pen_per_90": _per90(touches_att_pen, mins),
                    "progressive_carries": prog_carries,
                    "progressive_carries_per_90": _per90(prog_carries, mins),
                    "carries_into_final_third": cpa,
                    "carries_into_penalty_area": _safe_int(row.get("CPA")),
                    "cpa_per_90": _per90(_safe_int(row.get("CPA")), mins),
                    "progressive_passes_received": _safe_int(row.get("PrgR")),
                })
    except Exception:
        log.warning("fbref_possession_failed", exc_info=True)

    # --- Defensive actions (for CBIT/CBIRT modeling) ---
    try:
        defense = fbref.read_player_season_stats(stat_type="defense")
        if defense is not None and not defense.empty:
            for idx, row in defense.iterrows():
                player_name = idx[-1] if isinstance(idx, tuple) else str(idx)
                mins = 0
                for s in stats:
                    if normalize(s["name"]) == normalize(player_name):
                        mins = s.get("minutes", 0)
                        break
                tackles = _safe_int(row.get("Tkl"))
                tackles_won = _safe_int(row.get("TklW"))
                interceptions = _safe_int(row.get("Int"))
                blocks = _safe_int(row.get("Blocks"))
                clearances = _safe_int(row.get("Clr"))
                # CBIT = Clearances + Blocks + Interceptions + Tackles
                cbit = clearances + blocks + interceptions + tackles
                games = max(mins / 90, 1) if mins else 1
                _merge_by_name(stats, player_name, {
                    "tackles": tackles,
                    "tackles_won": tackles_won,
                    "interceptions": interceptions,
                    "blocks": blocks,
                    "clearances": clearances,
                    "cbit_total": cbit,
                    "cbit_per_game": round(cbit / games, 1),
                    "pressures": _safe_int(row.get("Press")),
                    "pressure_success_pct": _safe_float(row.get("Press%")),
                })
    except Exception:
        log.warning("fbref_defense_failed", exc_info=True)

    return stats


async def enrich(player_name: str) -> dict[str, Any] | None:
    """Get FBref stats for a player by name match."""
    data = await _load_data()
    if not data:
        return None

    matched = match_player(player_name, data, name_keys=("name",))
    if not matched:
        return None

    result = {"source": "fbref"}
    # Include all available keys except 'name'
    for key, val in matched.items():
        if key != "name" and val is not None:
            result[key] = val

    return result if len(result) > 1 else None

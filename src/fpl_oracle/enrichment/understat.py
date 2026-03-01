"""Understat data loader with Redis caching."""

from __future__ import annotations

import asyncio
from typing import Any

from fpl_oracle import cache
from fpl_oracle.enrichment.matcher import match_player
from fpl_oracle.log import get_logger

log = get_logger(__name__)

_CACHE_KEY = "fpl:enrichment:understat_epl"
_CACHE_TTL = 86400  # 24 hours
_data: list[dict[str, Any]] | None = None


async def _load_data() -> list[dict[str, Any]]:
    """Load Understat EPL data, with Redis cache."""
    global _data
    if _data:
        return _data

    # Try Redis
    cached = await cache.get(_CACHE_KEY)
    if cached and isinstance(cached, list):
        _data = cached
        return _data

    # Fetch from Understat API
    try:
        from understatapi import UnderstatClient

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_understat)
        if data:
            _data = data
            await cache.set(_CACHE_KEY, data, ttl=_CACHE_TTL)
            log.info("understat_loaded", count=len(data))
        return _data or []
    except Exception:
        log.warning("understat_load_failed", exc_info=True)
        return []


def _fetch_understat() -> list[dict[str, Any]]:
    with UnderstatClient() as client:
        raw = client.league(league="EPL").get_player_data(season="2024")
    return raw if isinstance(raw, list) else []


async def enrich(player_name: str) -> dict[str, Any] | None:
    """Get Understat stats for a player by name match."""
    data = await _load_data()
    if not data:
        return None

    matched = match_player(player_name, data, name_keys=("player_name",))
    if not matched:
        return None

    minutes = int(matched.get("time", 0) or 0)
    if minutes < 90:
        return None

    games = int(matched.get("games", 1) or 1)
    per90_factor = 90 / (minutes / games) if games > 0 else 1.0

    goals = int(matched.get("goals", 0) or 0)
    shots = int(matched.get("shots", 0) or 0)
    xg = float(matched.get("xG", 0) or 0)
    xa = float(matched.get("xA", 0) or 0)
    npxg = float(matched.get("npxG", 0) or 0)
    key_passes = int(matched.get("key_passes", 0) or 0)
    assists = int(matched.get("assists", 0) or 0)

    return {
        "source": "understat",
        "games": games,
        "minutes": minutes,
        "goals": goals,
        "assists": assists,
        "xg": round(xg, 2),
        "xa": round(xa, 2),
        "npxg": round(npxg, 2),
        "shots": shots,
        "key_passes": key_passes,
        "shots_per_90": round(shots / games * per90_factor, 2) if games else 0,
        "xg_per_90": round(xg / games * per90_factor, 2) if games else 0,
        "xa_per_90": round(xa / games * per90_factor, 2) if games else 0,
        "xg_per_shot": round(xg / shots, 3) if shots else 0,
        "npxg_per_90": round(npxg / games * per90_factor, 2) if games else 0,
        "key_passes_per_90": round(key_passes / games * per90_factor, 2) if games else 0,
        "goals_minus_xg": round(goals - xg, 2),
        "assists_minus_xa": round(assists - xa, 2),
        "conversion_rate": round(goals / shots * 100, 1) if shots else 0,
        "xg_chain": round(float(matched.get("xGChain", 0) or 0), 2),
        "xg_buildup": round(float(matched.get("xGBuildup", 0) or 0), 2),
        # Per-90 chain/buildup
        "xg_chain_per_90": round(float(matched.get("xGChain", 0) or 0) / games * per90_factor, 2) if games else 0,
        "xg_buildup_per_90": round(float(matched.get("xGBuildup", 0) or 0) / games * per90_factor, 2) if games else 0,
        # Regression indicators
        "npg": int(matched.get("npg", 0) or 0),
        "npg_minus_npxg": round(int(matched.get("npg", 0) or 0) - npxg, 2),
        # xGI combined
        "xgi": round(xg + xa, 2),
        "xgi_per_90": round((xg + xa) / games * per90_factor, 2) if games else 0,
    }

from __future__ import annotations

from typing import Any

import httpx
from cachetools import TTLCache

from fpl_oracle.config import settings
from fpl_oracle import cache
from fpl_oracle.log import get_logger

log = get_logger(__name__)

_client: httpx.AsyncClient | None = None
_mem: TTLCache[str, Any] = TTLCache(maxsize=256, ttl=120)

# TTLs in seconds
_TTLS = {
    "bootstrap": 120,
    "fixtures": 300,
    "element-summary": 1800,
    "live": 30,
    "manager": 60,
}


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.fpl_base_url,
            timeout=20,
            headers={"User-Agent": "FPLOracle/1.0"},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _fetch(endpoint: str, *, cache_key: str | None = None, ttl: int = 120) -> Any:
    # Memory cache
    if cache_key and cache_key in _mem:
        return _mem[cache_key]

    # Redis cache
    if cache_key:
        cached = await cache.get(cache_key)
        if cached is not None:
            _mem[cache_key] = cached
            return cached

    client = await _http()
    resp = await client.get(endpoint)
    resp.raise_for_status()
    data = resp.json()

    if cache_key:
        _mem[cache_key] = data
        await cache.set(cache_key, data, ttl=ttl)

    return data


async def bootstrap() -> dict[str, Any]:
    return await _fetch(
        "/bootstrap-static/", cache_key="fpl:bootstrap", ttl=_TTLS["bootstrap"]
    )


async def fixtures() -> list[dict[str, Any]]:
    return await _fetch(
        "/fixtures/", cache_key="fpl:fixtures:all", ttl=_TTLS["fixtures"]
    )


async def element_summary(player_id: int) -> dict[str, Any]:
    return await _fetch(
        f"/element-summary/{player_id}/",
        cache_key=f"fpl:element:{player_id}",
        ttl=_TTLS["element-summary"],
    )


async def event_live(event_id: int) -> dict[str, Any]:
    return await _fetch(
        f"/event/{event_id}/live/",
        cache_key=f"fpl:live:{event_id}",
        ttl=_TTLS["live"],
    )


async def manager_info(manager_id: int) -> dict[str, Any]:
    return await _fetch(
        f"/entry/{manager_id}/",
        cache_key=f"fpl:manager:{manager_id}",
        ttl=_TTLS["manager"],
    )


async def manager_history(manager_id: int) -> dict[str, Any]:
    return await _fetch(f"/entry/{manager_id}/history/")


async def manager_picks(manager_id: int, event: int) -> dict[str, Any]:
    return await _fetch(f"/entry/{manager_id}/event/{event}/picks/")


async def manager_transfers(manager_id: int) -> list[dict[str, Any]]:
    return await _fetch(f"/entry/{manager_id}/transfers/")

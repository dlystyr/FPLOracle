from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis

from fpl_oracle.config import settings
from fpl_oracle.log import get_logger

log = get_logger(__name__)

_pool: aioredis.Redis | None = None
_has_json: bool = False


def _serializer(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Not serializable: {type(obj)}")


async def init_cache() -> None:
    global _pool, _has_json
    _pool = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await _pool.execute_command("JSON.SET", "_probe", "$", '"ok"')
        await _pool.delete("_probe")
        _has_json = True
        log.info("redis_json_available")
    except Exception:
        _has_json = False
        log.info("redis_json_unavailable_using_strings")


async def close_cache() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


def _redis() -> aioredis.Redis:
    if _pool is None:
        raise RuntimeError("Cache not initialised – call init_cache() first")
    return _pool


async def get(key: str) -> Any | None:
    r = _redis()
    try:
        if _has_json:
            val = await r.execute_command("JSON.GET", key, "$")
            if val:
                parsed = json.loads(val)
                return parsed[0] if isinstance(parsed, list) else parsed
            return None
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        log.warning("cache_get_error", key=key, exc_info=True)
        return None


async def set(key: str, value: Any, ttl: int = 3600) -> None:
    r = _redis()
    try:
        if _has_json:
            await r.execute_command(
                "JSON.SET", key, "$", json.dumps(value, default=_serializer)
            )
            if ttl:
                await r.expire(key, ttl)
        else:
            await r.set(
                key, json.dumps(value, default=_serializer), ex=ttl or None
            )
    except Exception:
        log.warning("cache_set_error", key=key, exc_info=True)


async def delete(key: str) -> None:
    try:
        await _redis().delete(key)
    except Exception:
        log.warning("cache_delete_error", key=key, exc_info=True)

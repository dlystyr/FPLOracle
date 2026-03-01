from __future__ import annotations

from typing import Any

import asyncpg

from fpl_oracle.config import settings
from fpl_oracle.log import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    log.info("db_pool_created")


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB not initialised – call init_db() first")
    return _pool


async def fetch_all(query: str, *args: Any) -> list[dict[str, Any]]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]


async def fetch_one(query: str, *args: Any) -> dict[str, Any] | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(query, *args)
        return dict(row) if row else None


async def execute(query: str, *args: Any) -> str:
    async with pool().acquire() as conn:
        return await conn.execute(query, *args)

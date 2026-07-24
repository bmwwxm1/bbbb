"""Distributed locks with TTL and ownership.

Two backends:
  - Redis (primary, when available) — explicit TTL, ownership token, renewal
  - PostgreSQL (fallback) — row-based locks in `distributed_locks` table

Both implement the same LockManager protocol.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete, select, text

from bot.models.database import DistributedLock, async_session

logger = logging.getLogger(__name__)


class LockManager(Protocol):
    async def acquire(self, key: str, ttl_seconds: int = 30, owner: str = "") -> bool: ...

    async def release(self, key: str, owner: str = "") -> bool: ...

    async def extend(self, key: str, ttl_seconds: int = 30) -> bool: ...


# ── Redis Backend ──────────────────────────────────────────────────────


class RedisLockManager:
    """Redis-based distributed locks. Requires aioredis/redis-py."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._redis: object | None = None

    async def _get_redis(self) -> object:
        if self._redis is None:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(self._url)
            except ImportError:
                raise RuntimeError("redis package not installed")
        return self._redis

    async def acquire(self, key: str, ttl_seconds: int = 30, owner: str = "") -> bool:
        r = await self._get_redis()
        owner = owner or str(uuid.uuid4())
        acquired = await r.set(  # type: ignore[union-attr]
            f"lock:{key}", owner, nx=True, ex=ttl_seconds
        )
        if acquired:
            logger.debug("Redis lock acquired: %s (ttl=%ds)", key, ttl_seconds)
        return bool(acquired)

    async def release(self, key: str, owner: str = "") -> bool:
        r = await self._get_redis()
        lua = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = await r.eval(lua, 1, f"lock:{key}", owner)  # type: ignore[union-attr]
        return bool(result)

    async def extend(self, key: str, ttl_seconds: int = 30) -> bool:
        r = await self._get_redis()
        result = await r.expire(f"lock:{key}", ttl_seconds)  # type: ignore[union-attr]
        return bool(result)


# ── PostgreSQL Backend ─────────────────────────────────────────────────


class PGLockManager:
    """PostgreSQL-based locks using distributed_locks table.

    Suitable for single-instance deployments. Locks have explicit TTL
    and ownership tokens. Expired locks are auto-cleaned on acquire.
    """

    async def acquire(self, key: str, ttl_seconds: int = 30, owner: str = "") -> bool:
        owner = owner or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        async with async_session() as session:
            async with session.begin():
                # Clean expired locks
                await session.execute(
                    delete(DistributedLock).where(DistributedLock.expires_at < now)
                )

                # Check if lock exists and is still valid
                existing = await session.execute(
                    select(DistributedLock).where(DistributedLock.lock_key == key)
                )
                lock = existing.scalar_one_or_none()

                if lock is not None:
                    if lock.expires_at > now:
                        return False  # Lock held by someone else
                    # Expired — remove and re-acquire
                    await session.execute(
                        delete(DistributedLock).where(DistributedLock.lock_key == key)
                    )

                new_lock = DistributedLock(
                    lock_key=key,
                    owner=owner,
                    acquired_at=now,
                    expires_at=expires_at,
                )
                session.add(new_lock)

        logger.debug("PG lock acquired: %s (ttl=%ds, owner=%s)", key, ttl_seconds, owner[:8])
        return True

    async def release(self, key: str, owner: str = "") -> bool:
        async with async_session() as session:
            async with session.begin():
                if owner:
                    result = await session.execute(
                        delete(DistributedLock).where(
                            DistributedLock.lock_key == key,
                            DistributedLock.owner == owner,
                        )
                    )
                else:
                    result = await session.execute(
                        delete(DistributedLock).where(DistributedLock.lock_key == key)
                    )
                released = result.rowcount > 0  # type: ignore[union-attr]

        if released:
            logger.debug("PG lock released: %s", key)
        return released

    async def extend(self, key: str, ttl_seconds: int = 30) -> bool:
        new_expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(
                    text("UPDATE distributed_locks SET expires_at = :exp WHERE lock_key = :key"),
                    {"exp": new_expires, "key": key},
                )
                return result.rowcount > 0  # type: ignore[union-attr]


# ── Factory ────────────────────────────────────────────────────────────


def create_lock_manager(redis_url: str | None = None) -> LockManager:
    """Create the best available lock manager."""
    if redis_url:
        try:
            return RedisLockManager(redis_url)
        except Exception:
            logger.warning("Redis unavailable, falling back to PG locks")
    return PGLockManager()

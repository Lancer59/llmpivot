"""
In-memory cache with TTL-based background refresh.
Serves stale values if DB is unreachable.
"""

import asyncio
import logging
import time
from typing import Optional

from .db import fetch_active_version

logger = logging.getLogger("llmpivot.cache")


class PromptCache:
    def __init__(self, db_path: str, ttl: int):
        self._db_path = db_path
        self._ttl = ttl
        # { name: {"content": str, "version_id": int, "fetched_at": float} }
        self._store: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start background refresh loop. Call once from async context."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ttl)
            for name in list(self._store.keys()):
                await self._fetch_and_store(name)

    async def get(self, name: str) -> Optional[dict]:
        """Return cached entry, refreshing if stale."""
        entry = self._store.get(name)
        if entry is None or (time.monotonic() - entry["fetched_at"]) > self._ttl:
            await self._fetch_and_store(name)
        return self._store.get(name)

    async def _fetch_and_store(self, name: str) -> None:
        try:
            row = await fetch_active_version(self._db_path, name)
            if row:
                self._store[name] = {
                    "content": row["content"],
                    "version_id": row["id"],
                    "fetched_at": time.monotonic(),
                }
        except Exception as exc:
            logger.warning("Cache refresh failed for '%s': %s", name, exc)
            # Keep stale value - do not evict

    def invalidate(self, name: str) -> None:
        """Force next get() to re-fetch from DB."""
        self._store.pop(name, None)

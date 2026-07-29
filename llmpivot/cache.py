"""
In-memory cache with TTL-based background refresh.
Serves stale values if DB is unreachable.
"""

import asyncio
import logging
import time
from typing import Optional, Union

from .storage import BaseStorage, SQLiteStorage

logger = logging.getLogger("llmpivot.cache")


class PromptCache:
    def __init__(self, storage: Union[BaseStorage, str], ttl: int, tenant_id: str = "default"):
        if isinstance(storage, str):
            self.storage = SQLiteStorage(storage)
            self.storage.init_db_sync()
        else:
            self.storage = storage

        self._ttl = ttl
        self._tenant_id = tenant_id
        # { name: {"content": str, "version_id": int, "fetched_at": float} }
        self._store: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start background refresh loop. Call once from async context."""
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._refresh_loop())
            except RuntimeError:
                pass

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
            row = await self.storage.fetch_active_version(name, tenant_id=self._tenant_id)
            if row:
                self._store[name] = {
                    "content": row["content"],
                    "version_id": row["id"],
                    "fetched_at": time.monotonic(),
                }
        except Exception as exc:
            logger.warning("Cache refresh failed for '%s': %s", name, exc)

    def invalidate(self, name: str) -> None:
        """Force next get() to re-fetch from DB."""
        self._store.pop(name, None)

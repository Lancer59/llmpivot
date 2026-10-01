"""
Async, queue-buffered prompt usage logger with batch writing support.
Handles high write concurrency safely without blocking API endpoints or locking databases.

Production notes:
- Queue is bounded (maxsize=10_000) to prevent unbounded memory growth under write storms.
- Items are dropped (with a warning) when the queue is full rather than blocking callers.
- Graceful shutdown: call await stop() to drain remaining items before process exit.
"""

import asyncio
import logging
import random
import time
from typing import Optional, Union
from .storage import BaseStorage, SQLiteStorage

logger = logging.getLogger("llmpivot.logger")

# Maximum number of items held in memory before new entries are dropped.
_QUEUE_MAXSIZE = 10_000


class PromptLogger:
    def __init__(
        self,
        storage: Union[BaseStorage, str],
        sample_rate: float = 1.0,
        tenant_id: str = "default",
        batch_size: int = 50,
        flush_interval: float = 2.0,
    ):
        if isinstance(storage, str):
            self.storage = SQLiteStorage(storage)
            self.storage.init_db_sync()
        else:
            self.storage = storage

        self._sample_rate = max(0.0, min(1.0, sample_rate))
        self._tenant_id = tenant_id
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        # Bounded queue — prevents OOM on write storms.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the background batch writer worker in an active event loop."""
        if self._worker_task is None or self._worker_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._worker_task = loop.create_task(self._batch_worker())
            except RuntimeError:
                pass

    def stop(self) -> None:
        """Cancel the background worker immediately (non-graceful)."""
        if self._worker_task:
            self._worker_task.cancel()

    async def drain(self) -> None:
        """
        Gracefully flush all remaining queued items to storage.
        Call this during application shutdown before cancelling the worker.
        Waits up to 10 seconds before giving up.
        """
        try:
            if not self._queue.empty():
                await asyncio.wait_for(self._flush_remaining(), timeout=10.0)
            await asyncio.wait_for(self._queue.join(), timeout=10.0)
        except asyncio.TimeoutError:
            remaining = self._queue.qsize()
            logger.warning(
                "Logger drain timed out; %d log entries may be lost on shutdown.", remaining
            )

    async def _flush_remaining(self) -> None:
        """Drain everything currently in the queue in one or more batches."""
        while not self._queue.empty():
            batch = []
            while len(batch) < self._batch_size and not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    break
            if batch:
                try:
                    await self.storage.insert_logs_batch(batch)
                except Exception as exc:
                    logger.warning("Failed to flush log batch on drain: %s", exc)
                finally:
                    for _ in batch:
                        self._queue.task_done()

    async def flush(self) -> None:
        """Wait until all currently queued usage logs have been persisted."""
        if not self._queue.empty():
            self.start()
        await asyncio.wait_for(self._queue.join(), timeout=10.0)

    def log(
        self,
        prompt_name: str,
        version_id: int,
        input_text: str,
        output_text: str,
    ) -> None:
        """Enqueue usage log entry. Non-blocking and fire-and-forget."""
        if self._sample_rate < 1.0 and random.random() > self._sample_rate:
            return

        item = {
            "prompt_name": prompt_name,
            "version_id": version_id,
            "input_text": input_text,
            "output_text": output_text,
            "tenant_id": self._tenant_id,
            "timestamp": time.time(),
        }

        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    logger.warning(
                        "Usage log queue full (%d items); dropping log entry for prompt '%s'. "
                        "Consider increasing flush frequency or batch size.",
                        _QUEUE_MAXSIZE,
                        prompt_name,
                    )
                    return
                self.start()
                return
        except RuntimeError:
            pass  # no running loop — fall through to sync path

        # Sync path: no event loop running (e.g. called from a plain script)
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.storage.insert_logs_batch([item]))
            finally:
                loop.close()
        except Exception as exc:
            logger.debug("Could not write log synchronously: %s", exc)

    async def _batch_worker(self) -> None:
        while True:
            batch = []
            try:
                # Wait for at least one item
                first_item = await asyncio.wait_for(
                    self._queue.get(), timeout=self._flush_interval
                )
                batch.append(first_item)

                # Drain up to batch_size more
                while len(batch) < self._batch_size and not self._queue.empty():
                    item = self._queue.get_nowait()
                    batch.append(item)

            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                # Flush whatever is in-hand before exiting
                if batch:
                    try:
                        await self.storage.insert_logs_batch(batch)
                    except Exception as exc:
                        logger.warning("Failed to flush batch on cancel: %s", exc)
                    finally:
                        for _ in batch:
                            self._queue.task_done()
                break
            except Exception as exc:
                logger.debug("Error in batch log worker: %s", exc)

            if batch:
                try:
                    await self.storage.insert_logs_batch(batch)
                except Exception as exc:
                    logger.warning("Failed to flush log batch: %s", exc)
                finally:
                    for _ in batch:
                        self._queue.task_done()

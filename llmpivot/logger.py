"""
Async, queue-buffered prompt usage logger with batch writing support.
Handles high write concurrency safely without blocking API endpoints or locking databases.
"""

import asyncio
import logging
import random
import time
from typing import Optional, Union
from .storage import BaseStorage, SQLiteStorage

logger = logging.getLogger("llmpivot.logger")


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

        self._queue: asyncio.Queue = asyncio.Queue()
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
        if self._worker_task:
            self._worker_task.cancel()

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
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._queue.put_nowait(item)
                self.start()
            else:
                loop.run_until_complete(self.storage.insert_logs_batch([item]))
        except Exception as exc:
            logger.debug("Could not schedule log enqueue: %s", exc)

    async def _batch_worker(self) -> None:
        while True:
            batch = []
            try:
                # Wait for at least one item
                first_item = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
                batch.append(first_item)
                self._queue.task_done()

                # Drain up to batch_size
                while len(batch) < self._batch_size and not self._queue.empty():
                    item = self._queue.get_nowait()
                    batch.append(item)
                    self._queue.task_done()

            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Error in batch log worker: %s", exc)

            if batch:
                try:
                    await self.storage.insert_logs_batch(batch)
                except Exception as exc:
                    logger.warning("Failed to flush log batch: %s", exc)

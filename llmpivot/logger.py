"""
Async, non-blocking prompt usage logger with sampling support.
Failures are silently swallowed — never propagate to caller.
"""

import asyncio
import logging
import random

from .db import fetch_prompt_id, insert_log

logger = logging.getLogger("llmpivot.logger")


class PromptLogger:
    def __init__(self, db_path: str, sample_rate: float = 1.0):
        self._db_path = db_path
        self._sample_rate = max(0.0, min(1.0, sample_rate))

    def log(
        self,
        prompt_name: str,
        version_id: int,
        input_text: str,
        output_text: str,
    ) -> None:
        """Fire-and-forget log. Safe to call from sync or async code."""
        if self._sample_rate < 1.0 and random.random() > self._sample_rate:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    self._write(prompt_name, version_id, input_text, output_text)
                )
            else:
                loop.run_until_complete(
                    self._write(prompt_name, version_id, input_text, output_text)
                )
        except Exception as exc:
            logger.debug("Could not schedule log write: %s", exc)

    async def _write(
        self,
        prompt_name: str,
        version_id: int,
        input_text: str,
        output_text: str,
    ) -> None:
        try:
            prompt_id = await fetch_prompt_id(self._db_path, prompt_name)
            if prompt_id is None:
                return
            await insert_log(self._db_path, prompt_id, version_id, input_text, output_text)
        except RuntimeError:
            # Event loop closed during shutdown — safe to ignore
            pass
        except Exception as exc:
            logger.debug("Log write failed silently: %s", exc)

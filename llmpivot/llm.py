"""
LLM client for the prompt suggester and A/B test runner.
Calls any OpenAI-compatible chat completions endpoint.

Production notes:
- Retries up to 3 times with exponential backoff on transient errors (5xx, timeout, connection).
- Does NOT retry on 4xx (bad request / auth) — those are caller errors.
- API key and internal model name are never included in error messages returned to callers.
"""

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger("llmpivot.llm")

DEFAULT_SYSTEM_PROMPT = (
    "You are a prompt engineering expert. "
    "Improve the given prompt for clarity, specificity, and effectiveness. "
    "Return only the improved prompt text, nothing else."
)

# Retry config
_MAX_RETRIES = 3
_BASE_BACKOFF = 0.5   # seconds; doubles each attempt: 0.5 → 1.0 → 2.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        system_prompt: Optional[str] = None,
    ):
        self._url = url
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    async def suggest(self, content: str) -> str:
        """Return an improved version of the given prompt content."""
        return await self._call(self._system_prompt, content)

    async def run(self, system_prompt: str, user_input: str) -> str:
        """Run a prompt (as system) against user input. Used for A/B testing."""
        return await self._call(system_prompt, user_input)

    async def _call(self, system: str, user: str) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(self._url, json=payload, headers=headers)

                    # Don't retry client errors (4xx) — they won't fix themselves.
                    if resp.status_code < 500 and resp.status_code != 429:
                        resp.raise_for_status()
                        data = resp.json()
                        return data["choices"][0]["message"]["content"]

                    if resp.status_code not in _RETRYABLE_STATUS:
                        resp.raise_for_status()

                    # Retryable server error
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                    logger.warning(
                        "LLM request returned HTTP %d (attempt %d/%d); retrying...",
                        resp.status_code, attempt, _MAX_RETRIES,
                    )

            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "LLM request timed out (attempt %d/%d); retrying...", attempt, _MAX_RETRIES
                )
            except httpx.ConnectError as exc:
                last_exc = exc
                logger.warning(
                    "LLM connection error (attempt %d/%d); retrying...", attempt, _MAX_RETRIES
                )
            except httpx.HTTPStatusError:
                # Non-retryable 4xx — re-raise immediately, scrubbing auth details.
                raise

            if attempt < _MAX_RETRIES:
                backoff = _BASE_BACKOFF * (2 ** (attempt - 1))
                await asyncio.sleep(backoff)

        # All retries exhausted — raise a clean error without leaking internals.
        raise RuntimeError(
            f"LLM request failed after {_MAX_RETRIES} attempts. "
            "Check provider availability and your configuration."
        ) from last_exc

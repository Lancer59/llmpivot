"""
LLM client for the prompt suggester and A/B test runner.
Calls any OpenAI-compatible chat completions endpoint.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("llmpivot.llm")

DEFAULT_SYSTEM_PROMPT = (
    "You are a prompt engineering expert. "
    "Improve the given prompt for clarity, specificity, and effectiveness. "
    "Return only the improved prompt text, nothing else."
)


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
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self._url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

"""
LLM client for the prompt suggester, A/B test runner, and Assistant agent.
Calls any OpenAI-compatible chat completions endpoint, with native support
for Azure OpenAI.

Supported api_type values:
  "openai"  — standard OpenAI or any OpenAI-compatible endpoint (Ollama, LM Studio, etc.)
              Uses Authorization: Bearer header, includes 'model' in body.
  "azure"   — Azure OpenAI deployments.
              Uses api-key header, omits 'model' from body (deployment is in the URL).
              Auto-detected when the endpoint URL contains openai.azure.com.

Token limit params:
  max_tokens            — used by GPT-4 and earlier models (default: 2048)
  max_completion_tokens — used by GPT-5, o1, o3+ series (400 error if max_tokens is sent instead)

Auto-detection of new-gen models:
  If neither max_tokens nor max_completion_tokens is set explicitly, the client
  inspects the model name and chooses the right field automatically. This can
  always be overridden via llm_max_tokens or llm_max_completion_tokens in LLMAssetManager.

Production notes:
- Retries up to 3 times with exponential backoff on transient errors (5xx, timeout, connection).
- Does NOT retry on 4xx (bad request / auth) — those are caller errors.
- API key is never included in error messages.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("llmpivot.llm")

DEFAULT_SYSTEM_PROMPT = (
    "You are a prompt engineering expert. "
    "Improve the given prompt for clarity, specificity, and effectiveness. "
    "Return only the improved prompt text, nothing else."
)

# Retry config
_MAX_RETRIES = 3
_BASE_BACKOFF = 0.5  # seconds; doubles each attempt: 0.5 → 1.0 → 2.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Model name prefixes that use max_completion_tokens instead of max_tokens
_NEW_GEN_PREFIXES = ("gpt-5", "o1", "o2", "o3", "o4", "gpt-o")


def _detect_api_type(url: str) -> str:
    """Auto-detect 'azure' or 'openai' from the URL."""
    if "openai.azure.com" in url or (".azure.com" in url and "openai" in url):
        return "azure"
    return "openai"


def _is_new_gen_model(model: str) -> bool:
    """Return True for models that require max_completion_tokens instead of max_tokens."""
    m = model.lower()
    return any(m.startswith(p) for p in _NEW_GEN_PREFIXES)


class LLMClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        system_prompt: Optional[str] = None,
        # API type: "openai" | "azure" | None (auto-detect from URL)
        api_type: Optional[str] = None,
        # Token limits — set one or the other, not both.
        # None = auto-select based on model name.
        max_tokens: Optional[int] = None,
        max_completion_tokens: Optional[int] = None,
        # Generation parameters — None = omit from request (some models reject them)
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        # Any extra body params to merge in (e.g. {"response_format": {"type": "json_object"}})
        extra_params: Optional[Dict[str, Any]] = None,
        # Request timeout in seconds
        timeout: float = 30.0,
    ):
        self._url = url
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._api_type = api_type or _detect_api_type(url)
        self._is_azure = self._api_type == "azure"

        # Token limit resolution:
        # Explicit wins. If neither is set, auto-detect from model name.
        if max_tokens is not None and max_completion_tokens is not None:
            raise ValueError(
                "Set either max_tokens or max_completion_tokens, not both. "
                "Use max_completion_tokens for GPT-5 / o1 / o3+ models."
            )
        self._max_tokens = max_tokens
        self._max_completion_tokens = max_completion_tokens
        self._auto_token_field = (max_tokens is None and max_completion_tokens is None)

        self._temperature = temperature
        self._top_p = top_p
        self._extra_params = extra_params or {}
        self._timeout = timeout

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._is_azure:
            # Azure OpenAI uses api-key header
            headers["api-key"] = self._api_key
        else:
            # Standard OpenAI and compatible endpoints use Bearer token
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(self, system: str, user: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        # Model field: Azure omits it (deployment is in the URL), OpenAI requires it
        if not self._is_azure:
            payload["model"] = self._model

        # Token limit field
        if self._max_completion_tokens is not None:
            payload["max_completion_tokens"] = self._max_completion_tokens
        elif self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        elif self._auto_token_field:
            # Auto-detect: new-gen models need max_completion_tokens
            if _is_new_gen_model(self._model):
                payload["max_completion_tokens"] = 2048
            else:
                payload["max_tokens"] = 2048

        # Optional generation params — only include when explicitly set
        # (new-gen models reject temperature/top_p, so we default to omitting them)
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._top_p is not None:
            payload["top_p"] = self._top_p

        # Merge any extra caller-supplied params last (they can override anything above)
        payload.update(self._extra_params)

        return payload

    async def suggest(self, content: str) -> str:
        """Return an improved version of the given prompt content."""
        return await self._call(self._system_prompt, content)

    async def run(self, system_prompt: str, user_input: str) -> str:
        """Run a prompt (as system) against user input. Used for A/B testing."""
        return await self._call(system_prompt, user_input)

    async def _call(self, system: str, user: str) -> str:
        headers = self._build_headers()
        payload = self._build_payload(system, user)

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._url, json=payload, headers=headers)

                    # Don't retry 4xx — they're caller errors that won't fix themselves.
                    if resp.status_code < 500 and resp.status_code != 429:
                        resp.raise_for_status()
                        data = resp.json()
                        return data["choices"][0]["message"]["content"]

                    if resp.status_code not in _RETRYABLE_STATUS:
                        resp.raise_for_status()

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
                raise

            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_BASE_BACKOFF * (2 ** (attempt - 1)))

        raise RuntimeError(
            f"LLM request failed after {_MAX_RETRIES} attempts. "
            "Check provider availability and your configuration."
        ) from last_exc

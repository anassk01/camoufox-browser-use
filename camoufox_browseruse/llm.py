"""Google Gemini integration supplied by Browser-Use, with optional key pooling."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from typing import Any

from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.exceptions import ModelProviderError

# Backward-compatible public name. Browser-Use owns message conversion,
# structured output, retry policy, and supported model handling.
DEFAULT_GEMINI_MODEL = 'gemini-3.8-flash'
GeminiChatModel = ChatGoogle


class GeminiKeyPoolChatModel(ChatGoogle):
    """Use one Gemini model across keys while honoring provider cooldowns.

    Browser-Use 0.12.x wraps Google SDK errors only after its internal retry
    loop, so SDK 429/503 responses otherwise reach the Agent immediately. This
    adapter keeps the normal ChatGoogle serialization and structured output,
    but rotates credentials and paces calls before surfacing an outage.
    """

    def __init__(
        self,
        *,
        api_keys: Sequence[str],
        min_request_interval: float = 13.0,
        pool_attempts: int = 16,
        **kwargs: Any,
    ) -> None:
        keys = tuple(dict.fromkeys(key.strip() for key in api_keys if key and key.strip()))
        if not keys:
            raise ValueError('At least one Gemini API key is required.')
        # Pooling owns retries. Keep each underlying key attempt singular.
        kwargs['max_retries'] = 1
        super().__init__(api_key=keys[0], **kwargs)
        self._pool_keys = keys
        self._pool_index = 0
        self._pool_cooldowns = [0.0] * len(keys)
        self._pool_min_interval = max(0.0, float(min_request_interval))
        self._pool_attempts = max(len(keys), int(pool_attempts))
        self._pool_last_request = 0.0
        self._pool_lock = asyncio.Lock()

    @staticmethod
    def _retry_delay(error: ModelProviderError) -> float:
        if 'PerDay' in error.message:
            # A different key from the same Google project shares this quota.
            # Keep trying the remaining keys once, then surface the provider
            # error rather than hammering a project-wide daily limit.
            return 86400.0
        match = re.search(r'retry in\s+([0-9]+(?:\.[0-9]+)?)s?', error.message, re.IGNORECASE)
        if match:
            return min(120.0, max(1.0, float(match.group(1)) + 1.0))
        return 8.0 if error.status_code == 503 else 60.0

    async def ainvoke(self, messages, output_format=None, **kwargs):
        async with self._pool_lock:
            last_error: ModelProviderError | None = None
            for _ in range(self._pool_attempts):
                now = time.monotonic()
                ready = [
                    index for index, until in enumerate(self._pool_cooldowns)
                    if until <= now
                ]
                if not ready:
                    wait = min(self._pool_cooldowns) - now
                    if wait > 120.0 and last_error is not None:
                        raise last_error
                    await asyncio.sleep(max(0.1, wait))
                    now = time.monotonic()
                    ready = [
                        index for index, until in enumerate(self._pool_cooldowns)
                        if until <= now
                    ]

                index = next(
                    (candidate for candidate in ready if candidate >= self._pool_index),
                    ready[0],
                )
                spacing = self._pool_min_interval - (time.monotonic() - self._pool_last_request)
                if spacing > 0:
                    await asyncio.sleep(spacing)
                self.api_key = self._pool_keys[index]
                self._client = None
                self._pool_index = (index + 1) % len(self._pool_keys)
                self._pool_last_request = time.monotonic()
                try:
                    return await super().ainvoke(messages, output_format, **kwargs)
                except ModelProviderError as error:
                    if error.status_code in {401, 403}:
                        raise
                    if error.status_code not in {429, 500, 502, 503, 504}:
                        raise
                    last_error = error
                    self._pool_cooldowns[index] = time.monotonic() + self._retry_delay(error)
            assert last_error is not None
            raise last_error


__all__ = ['DEFAULT_GEMINI_MODEL', 'GeminiChatModel', 'GeminiKeyPoolChatModel']

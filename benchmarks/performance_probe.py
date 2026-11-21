"""Timing helpers for comparing Browser-Use browser backends.

This module is deliberately outside the adapter package.  It observes public
Browser-Use boundaries without changing production behavior.
"""

from __future__ import annotations

import time
from math import ceil
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, AsyncIterator, TypeVar, overload

from browser_use.llm.messages import BaseMessage
from browser_use.llm.views import ChatInvokeCompletion
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class TimingSample:
    category: str
    name: str
    seconds: float
    details: dict[str, Any] = field(default_factory=dict)


class PerformanceProbe:
    """Collect wall-clock samples and produce a compact JSON-safe report."""

    def __init__(self) -> None:
        self.samples: list[TimingSample] = []

    @asynccontextmanager
    async def measure(
        self,
        category: str,
        name: str,
        **details: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        started = time.perf_counter()
        try:
            yield details
        finally:
            self.samples.append(
                TimingSample(
                    category=category,
                    name=name,
                    seconds=time.perf_counter() - started,
                    details=details,
                )
            )

    def report(self) -> dict[str, Any]:
        grouped: dict[tuple[str, str], list[TimingSample]] = defaultdict(list)
        for sample in self.samples:
            grouped[(sample.category, sample.name)].append(sample)

        summary: dict[str, dict[str, Any]] = {}
        for (category, name), samples in grouped.items():
            seconds = sorted(sample.seconds for sample in samples)
            p95_index = min(len(seconds) - 1, max(0, ceil(len(seconds) * 0.95) - 1))
            summary[f"{category}.{name}"] = {
                "count": len(seconds),
                "total_seconds": round(sum(seconds), 4),
                "median_seconds": round(median(seconds), 4),
                "p95_seconds": round(seconds[p95_index], 4),
            }
        return {
            "summary": summary,
            "samples": [asdict(sample) for sample in self.samples],
        }


class TimedChatModel:
    """Measure calls to any Browser-Use-compatible chat model."""

    def __init__(self, model: Any, probe: PerformanceProbe) -> None:
        self.wrapped = model
        self.probe = probe

    @property
    def model(self) -> str:
        return self.wrapped.model

    @property
    def provider(self) -> str:
        return self.wrapped.provider

    @property
    def name(self) -> str:
        return self.wrapped.name

    @property
    def model_name(self) -> str:
        return self.wrapped.model_name

    @overload
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: type[T],
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: type[T] | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        prompt_chars = sum(len(str(getattr(message, "content", ""))) for message in messages)
        async with self.probe.measure(
            "model",
            "ainvoke",
            message_count=len(messages),
            prompt_chars=prompt_chars,
        ) as details:
            response = await self.wrapped.ainvoke(messages, output_format=output_format, **kwargs)
            completion = response.completion
            details["action_count"] = len(getattr(completion, "action", []) or [])
            usage = getattr(response, "usage", None)
            details["input_tokens"] = getattr(usage, "prompt_tokens", 0) or 0
            details["output_tokens"] = getattr(usage, "completion_tokens", 0) or 0
            return response


def instrument_session(session: Any, probe: PerformanceProbe) -> None:
    """Time complete state observations through the public session method."""

    original = session.get_browser_state_summary

    async def timed_state(*args: Any, **kwargs: Any) -> Any:
        async with probe.measure(
            "browser",
            "state",
            include_screenshot=kwargs.get("include_screenshot", True),
        ):
            return await original(*args, **kwargs)

    object.__setattr__(session, "get_browser_state_summary", timed_state)


async def measure_observation_parts(
    session: Any,
    probe: PerformanceProbe,
    iterations: int,
) -> None:
    """Measure DOM, screenshot, and combined state using public contracts."""

    for _ in range(iterations):
        get_dom_state = getattr(session, "get_dom_state", None)
        if get_dom_state is not None:
            async with probe.measure("browser", "dom") as details:
                dom = await get_dom_state()
                details["elements"] = len(dom.selector_map)

        async with probe.measure("browser", "screenshot") as details:
            image = await session.take_screenshot(full_page=False)
            details["bytes"] = len(image)

        async with probe.measure("browser", "state_without_screenshot"):
            await session.get_browser_state_summary(include_screenshot=False)

        async with probe.measure("browser", "state_with_screenshot"):
            await session.get_browser_state_summary(include_screenshot=True)


__all__ = [
    "PerformanceProbe",
    "TimedChatModel",
    "instrument_session",
    "measure_observation_parts",
]

"""Native Camoufox backend for Browser-Use 0.13."""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from camoufox_browseruse.launcher import (
    ProfileLock,
    ProfileLockError,
    resolve_executable_path,
)
from camoufox_browseruse.session import CamoufoxSession, apply_camoufox_mcp_compat
from camoufox_browseruse.tools.overrides import register_camoufox_tools

if TYPE_CHECKING:
    from camoufox_browseruse.learning import ExperienceLearner
    from camoufox_browseruse.llm import GeminiChatModel, GeminiKeyPoolChatModel
    from camoufox_browseruse.runner import (
        BROWSER_OPERATION_INSTRUCTIONS,
        BrowserTaskRunner,
        RunLimits,
    )

DEFAULT_DATA_DIR = pathlib.Path.home() / ".camoufox_browseruse"
DEFAULT_PROFILE_DIR = DEFAULT_DATA_DIR / "profile"


@asynccontextmanager
async def open_camoufox_session(
    headless: bool = False,
    humanize: bool = True,
    user_data_dir: str | pathlib.Path | None = None,
    extra_args: list[str] | None = None,
    **kwargs: Any,
) -> AsyncIterator[CamoufoxSession]:
    """Start one CamoufoxSession and close it automatically when leaving context."""
    is_persistent = bool(kwargs.get("persistent_context", user_data_dir is not None))

    options: dict[str, Any] = {
        "headless": headless,
        "humanize": humanize,
        "persistent_context": is_persistent,
        "accept_downloads": True,
        "disable_coop": True,
        "enable_cache": True,
        "i_know_what_im_doing": True,
        # A caller using the context manager still owns the session until the
        # block exits; Agent.close() must not kill it early.
        "keep_alive": kwargs.get("keep_alive", True),
        **kwargs,
    }
    if is_persistent:
        options["user_data_dir"] = str(user_data_dir or DEFAULT_PROFILE_DIR)

    if extra_args:
        options["args"] = extra_args

    session = CamoufoxSession(**options)
    await session.start()
    try:
        yield session
    finally:
        try:
            keep_open_seconds = float(os.environ.get("CAMOUFOX_KEEP_OPEN_SECONDS", "0"))
            if keep_open_seconds > 0:
                await asyncio.sleep(min(keep_open_seconds, 600))
        finally:
            await session.kill()


def __getattr__(name: str) -> Any:
    """Load task and model integrations on first access."""
    if name in {'BrowserTaskRunner', 'RunLimits', 'BROWSER_OPERATION_INSTRUCTIONS'}:
        from camoufox_browseruse import runner
        return getattr(runner, name)
    if name == 'ExperienceLearner':
        from camoufox_browseruse.learning import ExperienceLearner
        return ExperienceLearner
    if name in {'GeminiChatModel', 'GeminiKeyPoolChatModel'}:
        try:
            from camoufox_browseruse.llm import GeminiChatModel, GeminiKeyPoolChatModel
        except ImportError as exc:
            raise ImportError(
                "GeminiChatModel requires the optional Gemini dependency. "
                "Install it with: pip install 'camoufox-browser-use[gemini]'"
            ) from exc
        return {
            'GeminiChatModel': GeminiChatModel,
            'GeminiKeyPoolChatModel': GeminiKeyPoolChatModel,
        }[name]
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


__all__ = [
    'BROWSER_OPERATION_INSTRUCTIONS',
    'BrowserTaskRunner',
    'CamoufoxSession',
    'ExperienceLearner',
    'GeminiChatModel',
    'GeminiKeyPoolChatModel',
    'ProfileLock',
    'ProfileLockError',
    'RunLimits',
    'apply_camoufox_mcp_compat',
    'open_camoufox_session',
    'register_camoufox_tools',
    'resolve_executable_path',
]

"""Playwright-backed watchdogs mirroring Browser-Use's component boundaries."""

from .actions import ActionWatchdog
from .crash import CrashWatchdog
from .diagnostics import DiagnosticsWatchdog
from .dom import DOMWatchdog
from .downloads import DownloadsWatchdog
from .popups import PopupsWatchdog
from .screenshots import ScreenshotWatchdog
from .security import SecurityWatchdog
from .storage import StorageStateWatchdog

__all__ = [
    'ActionWatchdog',
    'CrashWatchdog',
    'DiagnosticsWatchdog',
    'DOMWatchdog',
    'DownloadsWatchdog',
    'PopupsWatchdog',
    'ScreenshotWatchdog',
    'SecurityWatchdog',
    'StorageStateWatchdog',
]

"""Opt-in structured failure artifacts for native Camoufox sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from browser_use.browser.events import BrowserErrorEvent

from .base import NativeWatchdog

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = ('password', 'passwd', 'secret', 'token', 'cookie', 'authorization', 'api_key', 'apikey')


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value
        host = parsed.hostname or ''
        if parsed.port is not None:
            host = f'{host}:{parsed.port}'
        return urlunsplit((
            parsed.scheme,
            host,
            parsed.path,
            '[REDACTED]' if parsed.query else '',
            '[REDACTED]' if parsed.fragment else '',
        ))
    except Exception:
        return value


def _redact(value: Any, key: str = '') -> Any:
    if any(marker in key.lower() for marker in SENSITIVE_KEYS):
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(item_key): _redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and value.startswith(('http://', 'https://')):
        return _safe_url(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class DiagnosticsWatchdog(NativeWatchdog):
    """Persist bounded diagnostic bundles when BrowserErrorEvent is emitted."""

    HANDLERS = {BrowserErrorEvent: 'on_BrowserErrorEvent'}

    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.records: list[dict[str, Any]] = []
        self._sequence = 0
        self._capture_lock = asyncio.Lock()

    async def on_BrowserErrorEvent(self, event: BrowserErrorEvent) -> None:
        now = datetime.now(UTC)
        page = self.browser_session.session_manager.page
        record: dict[str, Any] = {
            'schema_version': 1,
            'timestamp': now.isoformat(),
            'event_id': event.event_id,
            'error_type': event.error_type,
            'message': event.message,
            'details': _redact(event.details),
            'target_id': self.browser_session.agent_focus_target_id,
            'url': _safe_url(page.url) if page and not page.is_closed() else None,
            'artifacts': {},
            'artifact_errors': [],
        }
        self.records.append(record)
        if len(self.records) > self.browser_session.diagnostics_max_entries:
            del self.records[:-self.browser_session.diagnostics_max_entries]
        output_root = self.browser_session.diagnostics_dir
        if output_root is None:
            return

        async with self._capture_lock:
            self._sequence += 1
            name = f"failure-{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{self._sequence:04d}-{event.event_id[-8:]}"
            output_dir = output_root / name
            output_dir.mkdir(parents=True, exist_ok=False)
            record['directory'] = str(output_dir)
            record_path = output_dir / 'event.json'
            self._write_record(record_path, record)

            if page and not page.is_closed():
                try:
                    screenshot = await asyncio.wait_for(
                        self.browser_session.take_screenshot(),
                        timeout=self.browser_session.diagnostics_timeout_seconds,
                    )
                    screenshot_path = output_dir / 'screenshot.png'
                    screenshot_path.write_bytes(screenshot)
                    record['artifacts']['screenshot'] = screenshot_path.name
                except Exception as exc:
                    record['artifact_errors'].append(f'screenshot: {type(exc).__name__}: {exc}')

                try:
                    state = await asyncio.wait_for(
                        self.browser_session.get_dom_state(),
                        timeout=self.browser_session.diagnostics_timeout_seconds,
                    )
                    dom_path = output_dir / 'dom.txt'
                    dom_path.write_text(state.llm_representation(), encoding='utf-8')
                    record['artifacts']['dom'] = dom_path.name
                    record['interactive_elements'] = len(state.selector_map)
                except Exception as exc:
                    record['artifact_errors'].append(f'dom: {type(exc).__name__}: {exc}')

            self._write_record(record_path, record)
            self._enforce_retention(output_root)

    @staticmethod
    def _write_record(path: Path, record: dict[str, Any]) -> None:
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')

    def _enforce_retention(self, output_root: Path) -> None:
        entries = sorted(
            (path for path in output_root.iterdir() if path.is_dir() and path.name.startswith('failure-')),
            key=lambda path: path.name,
        )
        for stale in entries[:-self.browser_session.diagnostics_max_entries]:
            try:
                shutil.rmtree(stale)
            except OSError as exc:
                logger.warning('Could not remove stale diagnostic bundle %s: %s', stale, exc)

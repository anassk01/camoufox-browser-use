"""Cookie and storage-state persistence."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from browser_use.browser.events import (
    LoadStorageStateEvent,
    SaveStorageStateEvent,
    StorageStateLoadedEvent,
    StorageStateSavedEvent,
)

from .base import NativeWatchdog

STORAGE_RESTORE_MARKER = '__camoufox_storage_state_restore__'


class StorageStateWatchdog(NativeWatchdog):
    HANDLERS = {
        SaveStorageStateEvent: 'on_SaveStorageStateEvent',
        LoadStorageStateEvent: 'on_LoadStorageStateEvent',
    }

    async def cookies(self) -> list[dict[str, Any]]:
        return await (await self.browser_session.ensure_context()).cookies()

    async def clear_cookies(self) -> None:
        await (await self.browser_session.ensure_context()).clear_cookies()

    async def export(self, output_path: str | Path | None = None) -> dict[str, Any]:
        state = await (await self.browser_session.ensure_context()).storage_state()
        for origin in state.get('origins', []):
            origin['localStorage'] = [
                item for item in origin.get('localStorage', [])
                if item.get('name') != STORAGE_RESTORE_MARKER
            ]
        if output_path:
            Path(output_path).write_text(json.dumps(state, indent=2), encoding='utf-8')
        return state

    async def apply(self, storage_state: str | Path | dict[str, Any]) -> tuple[int, int]:
        context = self.browser_session.session_manager.context
        if context is None:
            raise RuntimeError('Cannot restore storage before the browser context exists.')
        if isinstance(storage_state, dict):
            state = storage_state
        else:
            path = Path(storage_state).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f'Storage state file not found: {path}')
            try:
                state = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f'Invalid storage state file: {path}') from exc
        if not isinstance(state, dict):
            raise ValueError('Storage state must be a mapping with cookies and origins.')
        cookies = state.get('cookies') or []
        origins = state.get('origins') or []
        if not isinstance(cookies, list) or not isinstance(origins, list):
            raise ValueError('Storage state cookies and origins must be lists.')
        if cookies:
            await context.add_cookies(cookies)

        for origin_data in origins:
            if not isinstance(origin_data, dict):
                continue
            origin = origin_data.get('origin')
            local_storage = origin_data.get('localStorage') or []
            if not origin or not isinstance(local_storage, list):
                continue
            items = {
                str(item['name']): str(item['value'])
                for item in local_storage
                if isinstance(item, dict)
                and 'name' in item
                and 'value' in item
                and item['name'] != STORAGE_RESTORE_MARKER
            }
            if not items:
                continue
            token = str(uuid.uuid4())
            script = f"""(() => {{
                if (window.location.origin === {json.dumps(str(origin))}) {{
                    const marker = {json.dumps(STORAGE_RESTORE_MARKER)};
                    const token = {json.dumps(token)};
                    if (localStorage.getItem(marker) !== token) {{
                        for (const [key, value] of Object.entries({json.dumps(items)})) {{
                            localStorage.setItem(key, value);
                        }}
                        localStorage.setItem(marker, token);
                    }}
                }}
            }})();"""
            await context.add_init_script(script)
            for page in context.pages:
                try:
                    if await page.evaluate('() => window.location.origin') == origin:
                        await page.evaluate(
                            """data => {
                                for (const [key, value] of Object.entries(data.items)) {
                                    localStorage.setItem(key, value);
                                }
                                localStorage.setItem(data.marker, data.token);
                            }""",
                            {'items': items, 'marker': STORAGE_RESTORE_MARKER, 'token': token},
                        )
                except Exception:
                    pass
        return len(cookies), len(origins)

    async def on_SaveStorageStateEvent(self, event: SaveStorageStateEvent) -> StorageStateSavedEvent:
        path = getattr(event, 'path', None) or getattr(event, 'output_path', None) or 'storage_state.json'
        state = await self.export(path)
        notification = StorageStateSavedEvent(
            path=str(path),
            cookies_count=len(state.get('cookies', [])),
            origins_count=len(state.get('origins', [])),
        )
        await self.event_bus.dispatch(notification)
        return notification

    async def on_LoadStorageStateEvent(self, event: LoadStorageStateEvent) -> StorageStateLoadedEvent:
        path = getattr(event, 'path', None)
        if not path or not Path(path).exists():
            notification = StorageStateLoadedEvent(path=str(path or ''), cookies_count=0, origins_count=0)
            await self.event_bus.dispatch(notification)
            return notification
        cookies, origins = await self.apply(path)
        notification = StorageStateLoadedEvent(path=str(path), cookies_count=cookies, origins_count=origins)
        await self.event_bus.dispatch(notification)
        return notification

"""JavaScript dialog handling."""

from __future__ import annotations

import asyncio
import logging

from browser_use.browser.events import DialogOpenedEvent
from playwright.async_api import Dialog, Page

from .base import NativeWatchdog

logger = logging.getLogger(__name__)


class PopupsWatchdog(NativeWatchdog):
    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.closed_messages: list[str] = []

    def setup_page(self, page: Page) -> None:
        async def handle(dialog: Dialog) -> None:
            try:
                self.closed_messages.append(f'[{dialog.type}] {dialog.message}')
                if dialog.type in {'alert', 'confirm', 'beforeunload'}:
                    await dialog.accept()
                else:
                    await dialog.dismiss()
                await self.event_bus.dispatch(DialogOpenedEvent(
                    dialog_type=dialog.type,
                    message=dialog.message,
                    url=page.url,
                ))
            except Exception as exc:
                logger.debug('Dialog handling failed: %s', exc)

        page.on('dialog', lambda dialog: asyncio.create_task(handle(dialog)))

    def consume(self) -> list[str]:
        messages = list(self.closed_messages)
        self.closed_messages.clear()
        return messages

    def reset(self) -> None:
        self.closed_messages.clear()

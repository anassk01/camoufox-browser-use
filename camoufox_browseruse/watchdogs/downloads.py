"""Permanent download handling through Playwright events."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from browser_use.browser.events import DownloadProgressEvent, DownloadStartedEvent, FileDownloadedEvent
from playwright.async_api import Download, Page

from .base import NativeWatchdog

logger = logging.getLogger(__name__)


class DownloadsWatchdog(NativeWatchdog):
    HANDLERS = {FileDownloadedEvent: 'on_FileDownloadedEvent'}

    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.files: list[str] = []

    def setup_page(self, page: Page) -> None:
        page.on('download', lambda download: asyncio.create_task(self._save(download)))

    async def _save(self, download: Download) -> None:
        guid = uuid.uuid4().hex
        try:
            await self.event_bus.dispatch(DownloadStartedEvent(
                guid=guid,
                url=download.url,
                suggested_filename=download.suggested_filename or 'download',
            ))
            configured = self.browser_session.downloads_path
            directory = Path(configured) if configured else Path.cwd() / 'downloads'
            directory.mkdir(parents=True, exist_ok=True)
            name = Path(download.suggested_filename or 'download').name
            if name in {'', '.', '..'}:
                name = 'download'
            destination = directory / name
            counter = 1
            while destination.exists():
                destination = directory / f'{destination.stem}-{counter}{destination.suffix}'
                counter += 1
            await download.save_as(str(destination))
            path = str(destination.resolve())
            size = destination.stat().st_size
            await self.event_bus.dispatch(DownloadProgressEvent(
                guid=guid,
                received_bytes=size,
                total_bytes=size,
                state='completed',
            ))
            await self.event_bus.dispatch(
                FileDownloadedEvent(
                    guid=guid,
                    file_name=destination.name,
                    path=path,
                    url=download.url,
                    file_size=size,
                )
            )
        except Exception as exc:
            await self.event_bus.dispatch(DownloadProgressEvent(
                guid=guid,
                received_bytes=0,
                total_bytes=0,
                state='canceled',
            ))
            logger.debug('Download handling failed: %s', exc)

    async def on_FileDownloadedEvent(self, event: FileDownloadedEvent) -> None:
        if event.path and event.path not in self.files:
            self.files.append(event.path)

    def reset(self) -> None:
        self.files.clear()

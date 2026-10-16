"""Camoufox lifecycle and target ownership, parallel to Browser-Use SessionManager."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browser_use.browser.events import (
    AgentFocusChangedEvent,
    BrowserConnectedEvent,
    BrowserKillEvent,
    BrowserLaunchEvent,
    BrowserLaunchResult,
    BrowserStartEvent,
    BrowserStopEvent,
    BrowserStoppedEvent,
    TabClosedEvent,
    TabCreatedEvent,
    TargetCrashedEvent,
)
from browser_use.browser.views import TabInfo
from camoufox.async_api import AsyncCamoufox
from camoufox.utils import launch_options as build_camoufox_launch_options
from playwright.async_api import Browser, BrowserContext, Page

from camoufox_browseruse.launcher import (
    ProfileLock,
    prepare_launch_environment,
    resolve_executable_path,
)
from camoufox_browseruse.watchdogs.base import NativeWatchdog

if TYPE_CHECKING:
    from camoufox_browseruse.session import CamoufoxSession


FIREFOX_SUPPORTED_PERMISSIONS = frozenset({'geolocation', 'notifications', 'screen-wake-lock'})


@dataclass(slots=True)
class PageTarget:
    target_id: str
    type: str = 'page'
    url: str = ''
    title: str = ''

    @property
    def target_type(self) -> str:
        return self.type


class CamoufoxSessionManager(NativeWatchdog):
    """Own the browser process, context, pages, stable IDs, and agent focus."""

    HANDLERS = {
        BrowserStartEvent: 'on_BrowserStartEvent',
        BrowserStopEvent: 'on_BrowserStopEvent',
        BrowserLaunchEvent: 'on_BrowserLaunchEvent',
        BrowserKillEvent: 'on_BrowserKillEvent',
    }

    def __init__(self, browser_session: CamoufoxSession) -> None:
        super().__init__(browser_session)
        self.camoufox: AsyncCamoufox | None = None
        self.browser: Browser | BrowserContext | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._page_to_target_id: dict[Page, str] = {}
        self._target_id_to_page: dict[str, Page] = {}
        self._next_target = 0
        self._lifecycle_lock = asyncio.Lock()
        self._profile_lock: ProfileLock | None = None
        self._recovery_lock = asyncio.Lock()
        self.browser_config: dict[str, Any] = {}

    async def on_BrowserStartEvent(self, event: BrowserStartEvent) -> dict[str, str]:
        await self.launch()
        return {'backend': 'camoufox-playwright'}

    async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
        if self.browser_session.browser_profile.keep_alive and not event.force:
            await self.event_bus.dispatch(BrowserStoppedEvent(reason='Kept alive due to keep_alive=True'))
            return
        await self.close()

    async def on_BrowserLaunchEvent(self, event: BrowserLaunchEvent) -> BrowserLaunchResult:
        await self.launch()
        return BrowserLaunchResult(cdp_url='playwright://camoufox')

    async def on_BrowserKillEvent(self, event: BrowserKillEvent) -> None:
        await self.close()

    def _context_args(self, context_options: dict[str, Any]) -> dict[str, Any]:
        session = self.browser_session
        args: dict[str, Any] = {}
        option_names = (
            'viewport', 'no_viewport', 'extra_http_headers', 'locale',
            'timezone_id', 'geolocation', 'permissions', 'storage_state',
            'bypass_csp', 'color_scheme', 'record_har_path',
            'record_har_url_filter', 'record_video_dir', 'record_video_size',
            'proxy', 'accept_downloads', 'user_agent', 'java_script_enabled',
            'base_url', 'ignore_https_errors',
        )
        for name in option_names:
            value = context_options.get(name)
            if value is None:
                value = getattr(session.browser_profile, name, None)
            if value is None:
                continue
            if hasattr(value, 'model_dump'):
                value = value.model_dump(exclude_none=True)
            elif hasattr(value, 'dict') and callable(value.dict):
                value = value.dict(exclude_none=True)
            elif hasattr(value, 'width') and hasattr(value, 'height'):
                value = {'width': int(value.width), 'height': int(value.height)}
            if name in ('viewport', 'record_video_size') and isinstance(value, dict):
                value = {'width': int(value['width']), 'height': int(value['height'])}
            if name == 'permissions':
                value = [item for item in value if item in FIREFOX_SUPPORTED_PERMISSIONS]
            args[name] = value
        if context_options.get('headers') and 'extra_http_headers' not in args:
            args['extra_http_headers'] = context_options['headers']
        return args

    async def launch(self) -> None:
        async with self._lifecycle_lock:
            if self.context is None:
                await self._launch()

    async def _launch(self) -> None:
        session = self.browser_session
        launch = dict(session._launch_options)
        context_options = dict(session._context_options)
        user_data_dir = context_options.get('user_data_dir') or getattr(session.browser_profile, 'user_data_dir', None)
        persistent = launch.get('persistent_context')
        if persistent is None:
            persistent = bool(user_data_dir)
            launch['persistent_context'] = persistent
        if persistent and user_data_dir:
            profile_path = Path(user_data_dir).expanduser().resolve()
            launch['user_data_dir'] = str(profile_path)
        else:
            profile_path = None
            launch.pop('user_data_dir', None)

        executable_path = resolve_executable_path(launch.get('executable_path'))
        if executable_path is None:
            launch.pop('executable_path', None)
        else:
            launch['executable_path'] = executable_path
        launch['env'] = prepare_launch_environment(launch.get('env'))

        context_args = self._context_args(context_options)

        persistent_storage = context_args.pop('storage_state', None) if persistent else None
        if persistent:
            for name, value in context_args.items():
                launch.setdefault(name, value)

        if profile_path is not None:
            profile_lock = ProfileLock(profile_path)
            profile_lock.acquire()
            self._profile_lock = profile_lock

        try:
            # Build regular launch options outside the caller's default executor.
            # Camoufox manages the virtual display when requested.
            if launch.get('headless') == 'virtual':
                self.camoufox = AsyncCamoufox(**launch)
            else:
                config = dict(launch.pop('config', None) or {})
                launch.pop('persistent_context', None)
                prepared_launch = build_camoufox_launch_options(config=config, **launch)
                self.browser_config = dict(config)
                self.camoufox = AsyncCamoufox(
                    from_options=prepared_launch,
                    persistent_context=bool(persistent),
                )
            entered = await self.camoufox.__aenter__()
            self.browser = entered
            if isinstance(entered, BrowserContext):
                self.context = entered
                if headers := context_args.get('extra_http_headers'):
                    await entered.set_extra_http_headers(headers)
                if 'geolocation' in context_args:
                    await entered.set_geolocation(context_args['geolocation'])
            else:
                self.context = await entered.new_context(**context_args)

            await self.context.route('**/*', session.security_watchdog.route_request)
            self.context.on('page', self._on_new_page)
            for script in context_options.get('init_scripts', []):
                await self.context.add_init_script(script)
            if permissions := context_options.get('permissions'):
                supported = [item for item in permissions if item in FIREFOX_SUPPORTED_PERMISSIONS]
                if supported:
                    await self.context.grant_permissions(supported)

            if persistent_storage is not None:
                await session.storage_watchdog.apply(persistent_storage)

            page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self._register_page(page, focus=True)
            await self.event_bus.dispatch(BrowserConnectedEvent(cdp_url='playwright://camoufox'))
        except BaseException:
            await self._close()
            raise

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close()

    async def _close(self) -> None:
        was_connected = self.context is not None
        try:
            try:
                if self.context is not None and self.context is not self.browser:
                    with suppress(Exception):
                        await self.context.close()
            finally:
                if self.camoufox is not None:
                    await self._exit_camoufox(self.camoufox)
        finally:
            profile_lock, self._profile_lock = self._profile_lock, None
            self.camoufox = None
            self.browser = None
            self.context = None
            self.page = None
            self._page_to_target_id.clear()
            self._target_id_to_page.clear()
            self._next_target = 0
            self.browser_session.agent_focus_target_id = None
            if profile_lock is not None:
                profile_lock.release()
        if was_connected:
            await self.event_bus.dispatch(BrowserStoppedEvent(reason='Camoufox context closed'))

    async def _exit_camoufox(self, camoufox: AsyncCamoufox) -> None:
        exit_task = asyncio.create_task(camoufox.__aexit__(None, None, None))
        try:
            done, _ = await asyncio.wait({exit_task}, timeout=5.0)
            if not done:
                # Firefox can stop answering Browser.close. Terminate this
                # session's Playwright driver so shutdown remains bounded.
                process = camoufox._connection._transport._proc
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
        except Exception:
            pass
        finally:
            try:
                await asyncio.wait_for(asyncio.gather(exit_task, return_exceptions=True), timeout=2.0)
            except TimeoutError:
                exit_task.cancel()

    def target_id(self, page: Page) -> str:
        if page not in self._page_to_target_id:
            target_id = f'camoufox-tab-{self._next_target:04d}'
            self._next_target += 1
            self._page_to_target_id[page] = target_id
            self._target_id_to_page[target_id] = page
        return self._page_to_target_id[page]

    def find_page(self, target_id: str) -> Page | None:
        if self.context is None:
            return None
        page = self._target_id_to_page.get(target_id)
        if page is not None and not page.is_closed():
            return page
        suffix = str(target_id).removeprefix('camoufox-tab-').removeprefix('tab-')
        for index, candidate in enumerate(self.context.pages):
            candidate_id = self.target_id(candidate)
            if candidate_id == target_id or candidate_id.endswith(suffix):
                return candidate
            if suffix.isdigit() and int(suffix) == index:
                return candidate
        return None

    def _register_page(self, page: Page, *, focus: bool) -> None:
        target_id = self.target_id(page)
        if not getattr(page, '_camoufox_manager_registered', False):
            setattr(page, '_camoufox_manager_registered', True)
            page.on('close', lambda: self._on_page_closed(page))
            page.on('crash', lambda _: self.event_bus.dispatch(TargetCrashedEvent(
                target_id=target_id,
                error='Playwright reported that the page crashed',
            )))
            self.browser_session._setup_page_watchdogs(page)
            self.event_bus.dispatch(TabCreatedEvent(target_id=target_id, url=page.url or 'about:blank'))
        if focus:
            focus_changed = self.browser_session.agent_focus_target_id != target_id
            self.page = page
            self.browser_session.agent_focus_target_id = target_id
            if focus_changed:
                self.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url=page.url or 'about:blank'))

    def _on_new_page(self, page: Page) -> None:
        self._register_page(page, focus=True)
        asyncio.create_task(self.browser_session.security_watchdog.close_pending_popup(page))

    def _on_page_closed(self, page: Page) -> None:
        target_id = self._page_to_target_id.pop(page, None)
        if target_id:
            self._target_id_to_page.pop(target_id, None)
        if self.page is page and self.context is not None:
            pages = [candidate for candidate in self.context.pages if not candidate.is_closed()]
            self.page = pages[-1] if pages else None
            self.browser_session.agent_focus_target_id = self.target_id(self.page) if self.page else None

    async def recover_from_crash(self, target_id: str) -> None:
        """Remove a crashed page and recover logical focus exactly once."""
        async with self._recovery_lock:
            context = self.context
            if context is None:
                return
            crashed = self._target_id_to_page.pop(target_id, None)
            if crashed is not None:
                self._page_to_target_id.pop(crashed, None)
                if not crashed.is_closed():
                    try:
                        await asyncio.wait_for(crashed.close(), timeout=2.0)
                    except Exception:
                        pass
            if self.page is crashed:
                self.page = None
                self.browser_session.agent_focus_target_id = None

            candidates = [
                page for page in context.pages
                if page is not crashed and not page.is_closed()
            ]
            page = candidates[-1] if candidates else await context.new_page()
            self._register_page(page, focus=True)

    async def ensure_context(self) -> BrowserContext:
        if self.context is None:
            await self.browser_session.start()
        assert self.context is not None
        return self.context

    async def current_page(self) -> Page:
        context = await self.ensure_context()
        if self.page is None or self.page.is_closed():
            page = context.pages[-1] if context.pages else await context.new_page()
            self._register_page(page, focus=True)
        assert self.page is not None
        return self.page

    async def new_page(self, url: str | None = None) -> Page:
        context = await self.ensure_context()
        page = await context.new_page()
        self._register_page(page, focus=True)
        if url:
            await page.goto(url, wait_until='domcontentloaded')
        return page

    async def reload_unresponsive_page(self, page: Page) -> Page:
        """Replace a tab whose main thread stopped answering with a fresh load of its URL.

        Firefox keeps other tabs usable and can still close the frozen one, but
        cannot interrupt the script that froze it.
        """
        url = page.url
        fresh = await self.new_page()
        await asyncio.wait_for(self.close_page(page), timeout=10)
        if url and url != 'about:blank':
            await fresh.goto(url, wait_until='domcontentloaded')
        return fresh

    async def close_page(self, target: Page | str | None = None) -> None:
        context = await self.ensure_context()
        page = (
            target if isinstance(target, Page)
            else self.find_page(target) if isinstance(target, str)
            else await self.current_page()
        )
        if page is None:
            raise ValueError(f"Tab '{target}' not found to close.")
        closed_id = self.target_id(page)
        if len(context.pages) <= 1:
            replacement = await context.new_page()
            self._register_page(replacement, focus=True)
            await replacement.goto('about:blank')
        await page.close()
        self.event_bus.dispatch(TabClosedEvent(target_id=closed_id))
        if self.page is None and context.pages:
            self._register_page(context.pages[-1], focus=True)

    async def switch(self, target_id: str | None) -> str:
        if target_id is None:
            context = await self.ensure_context()
            page = context.pages[-1] if context.pages else None
        else:
            page = self.find_page(target_id)
        if page is None:
            raise ValueError(f"Tab with target_id '{target_id}' not found.")
        self._register_page(page, focus=True)
        # All adapter operations target ``self.page`` directly. Camoufox's
        # Firefox transport can leave Playwright's bring_to_front command
        # unresolved, especially headless, so logical focus is authoritative.
        return self.target_id(page)

    async def tabs(self) -> list[TabInfo]:
        if self.context is None:
            return []
        result = []
        for page in self.context.pages:
            try:
                title = await page.title()
            except Exception:
                title = ''
            result.append(TabInfo(url=page.url, title=title, target_id=self.target_id(page), parent_target_id=None))
        return result

    def targets(self) -> list[PageTarget]:
        if self.context is None:
            return []
        return [PageTarget(target_id=self.target_id(page), url=page.url) for page in self.context.pages]

    def get_target(self, target_id: str) -> PageTarget | None:
        """Expose the current native URL for upstream domain-scoped secrets."""
        page = self.find_page(target_id)
        if page is None:
            return None
        return PageTarget(target_id=self.target_id(page), url=page.url)

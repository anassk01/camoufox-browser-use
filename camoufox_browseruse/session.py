"""Browser-Use session facade backed by Camoufox watchdog components."""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Union, cast

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import (
    BrowserStartEvent,
    BrowserStateRequestEvent,
    BrowserStopEvent,
    ClickElementEvent,
    SaveStorageStateEvent,
)
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.views import BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, SerializedDOMState
from bubus import EventBus
from playwright.async_api import Browser, BrowserContext, Locator, Page

from camoufox_browseruse.helpers import NoFakeCDPClient, tab_target_id
from camoufox_browseruse.session_manager import CamoufoxSessionManager
from camoufox_browseruse.watchdogs import (
    ActionWatchdog,
    CrashWatchdog,
    DiagnosticsWatchdog,
    DOMWatchdog,
    DownloadsWatchdog,
    PopupsWatchdog,
    ScreenshotWatchdog,
    SecurityWatchdog,
    StorageStateWatchdog,
)

logger = logging.getLogger(__name__)


class CamoufoxSession(BrowserSession):
    """Thin Browser-Use facade coordinating Playwright-backed watchdogs."""

    def __init__(self, *, headless: bool | None = None, **launch_options: Any) -> None:
        browser_session_params = set(inspect.signature(BrowserSession.__init__).parameters) - {'self'}
        session_kwargs = {
            name: value
            for name, value in launch_options.items()
            if name in browser_session_params and value is not None
        }
        if headless is not None:
            session_kwargs['headless'] = headless
        super().__init__(**session_kwargs)
        # Camoufox always launches a local browser. Accepting cloud/remote
        # options would mark the session non-local, which upstream tools treat
        # as permission to pass arbitrary upload paths to a remote machine.
        if self.browser_profile.use_cloud or not self.browser_profile.is_local:
            raise ValueError('Browser-Use cloud and remote browsers are unsupported by Camoufox; it launches a local browser.')

        if 'block_ip_addresses' in launch_options:
            self.browser_profile.block_ip_addresses = bool(launch_options['block_ip_addresses'])
        if 'keep_alive' in launch_options:
            self.browser_profile.keep_alive = bool(launch_options['keep_alive'])

        # Camoufox and Playwright also accept some options exposed by Browser-Use.
        launch_names = {
            'args', 'channel', 'chromium_sandbox', 'env', 'executable_path',
            'headless', 'ignore_default_args', 'screen', 'traces_dir',
        }
        session_only = (set(BrowserProfile.model_fields) | browser_session_params) - launch_names
        session_only |= {
            'humanize', 'diagnostics_dir', 'diagnostics_max_entries', 'diagnostics_timeout_seconds',
        }
        context_names = {
            'accept_downloads', 'base_url', 'bypass_csp', 'color_scheme', 'downloads_path',
            'extra_http_headers', 'geolocation', 'headers', 'ignore_https_errors', 'init_scripts',
            'java_script_enabled', 'locale', 'permissions', 'proxy', 'record_har_content',
            'record_har_mode', 'record_har_path', 'record_har_url_filter', 'record_video_dir',
            'record_video_size', 'storage_state', 'timezone_id', 'user_agent', 'user_data_dir',
            'viewport', 'no_viewport',
        }
        # An explicit argument wins; otherwise honor the profile, which reads
        # BROWSER_USE_HEADLESS and a caller-supplied BrowserProfile(headless=...).
        launch_headless = headless if headless is not None else self.browser_profile.headless
        raw_options = {'headless': launch_headless, **launch_options}
        object.__setattr__(self, '_context_options', {
            name: value for name, value in raw_options.items()
            if name in context_names and value is not None
        })
        object.__setattr__(self, '_launch_options', {
            name: value for name, value in raw_options.items()
            if name not in context_names and name not in session_only and value is not None
        })
        object.__setattr__(
            self,
            '_humanize_interactions_native',
            bool(launch_options.get('humanize', False)),
        )
        object.__setattr__(self, '_downloads_path_native', str(launch_options.get('downloads_path') or '') or None)
        object.__setattr__(self, '_active_init_scripts', {})
        object.__setattr__(self, '_no_cdp_client', NoFakeCDPClient())
        diagnostics_dir = launch_options.get('diagnostics_dir')
        object.__setattr__(
            self,
            '_diagnostics_dir_native',
            Path(diagnostics_dir).expanduser().resolve() if diagnostics_dir else None,
        )
        object.__setattr__(
            self,
            '_diagnostics_max_entries_native',
            max(1, int(launch_options.get('diagnostics_max_entries', 20))),
        )
        object.__setattr__(
            self,
            '_diagnostics_timeout_seconds_native',
            max(0.1, float(launch_options.get('diagnostics_timeout_seconds', 3.0))),
        )

        # BrowserSession's default handlers are CDP-specific. Install a clean
        # event bus and the same concern-based architecture with native handlers.
        self.event_bus = EventBus()
        self.session_manager = CamoufoxSessionManager(self)
        object.__setattr__(self, '_security_watchdog_native', SecurityWatchdog(self))
        object.__setattr__(self, '_storage_watchdog_native', StorageStateWatchdog(self))
        object.__setattr__(self, '_dom_watchdog_native', DOMWatchdog(self))
        object.__setattr__(self, '_action_watchdog_native', ActionWatchdog(self))
        object.__setattr__(self, '_crash_watchdog_native', CrashWatchdog(self))
        object.__setattr__(self, '_diagnostics_watchdog_native', DiagnosticsWatchdog(self))
        object.__setattr__(self, '_downloads_watchdog_native', DownloadsWatchdog(self))
        object.__setattr__(self, '_popups_watchdog_native', PopupsWatchdog(self))
        object.__setattr__(self, '_screenshot_watchdog_native', ScreenshotWatchdog(self))
        self._dom_watchdog = self.dom_watchdog
        self._downloads_watchdog = self.downloads_watchdog
        self.attach_all_watchdogs_sync()

    @property
    def security_watchdog(self) -> SecurityWatchdog:
        return self._security_watchdog_native

    @property
    def storage_watchdog(self) -> StorageStateWatchdog:
        return self._storage_watchdog_native

    @property
    def browser_config(self) -> dict[str, Any]:
        """Generated native Camoufox identity, available for opt-in profile reuse."""
        return json.loads(json.dumps(self.session_manager.browser_config))

    @property
    def dom_watchdog(self) -> DOMWatchdog:
        return self._dom_watchdog_native

    @property
    def action_watchdog(self) -> ActionWatchdog:
        return self._action_watchdog_native

    @property
    def crash_watchdog(self) -> CrashWatchdog:
        return self._crash_watchdog_native

    @property
    def diagnostics_watchdog(self) -> DiagnosticsWatchdog:
        return self._diagnostics_watchdog_native

    @property
    def diagnostics_dir(self) -> Path | None:
        return self._diagnostics_dir_native

    @property
    def diagnostics_max_entries(self) -> int:
        return self._diagnostics_max_entries_native

    @property
    def diagnostics_timeout_seconds(self) -> float:
        return self._diagnostics_timeout_seconds_native

    @property
    def diagnostic_records(self) -> list[dict[str, Any]]:
        return list(self.diagnostics_watchdog.records)

    @property
    def downloads_watchdog(self) -> DownloadsWatchdog:
        return self._downloads_watchdog_native

    @property
    def popups_watchdog(self) -> PopupsWatchdog:
        return self._popups_watchdog_native

    @property
    def screenshot_watchdog(self) -> ScreenshotWatchdog:
        return self._screenshot_watchdog_native

    def attach_all_watchdogs_sync(self) -> None:
        for watchdog in (
            self.session_manager,
            self.crash_watchdog,
            self.diagnostics_watchdog,
            self.action_watchdog,
            self.dom_watchdog,
            self.storage_watchdog,
            self.downloads_watchdog,
            self.screenshot_watchdog,
        ):
            watchdog.attach()

    async def attach_all_watchdogs(self) -> None:
        self._dom_watchdog = self.dom_watchdog
        self._downloads_watchdog = self.downloads_watchdog

    def _setup_page_watchdogs(self, page: Page) -> None:
        self.security_watchdog.setup_page(page)
        self.popups_watchdog.setup_page(page)
        self.downloads_watchdog.setup_page(page)
        self.dom_watchdog.setup_page(page)

    @property
    def _camoufox(self):
        return self.session_manager.camoufox

    @property
    def _browser(self) -> Browser | BrowserContext | None:
        return self.session_manager.browser

    @property
    def _context(self) -> BrowserContext | None:
        return self.session_manager.context

    @property
    def _page(self) -> Page | None:
        return self.session_manager.page

    @property
    def _selector_targets(self) -> dict[int, dict[str, Any]]:
        return self.dom_watchdog.selector_targets

    @property
    def _closed_popup_messages(self) -> list[str]:
        return self.popups_watchdog.closed_messages

    @property
    def _browser_errors(self) -> list[str]:
        return self.security_watchdog.errors

    @staticmethod
    def _is_root_domain(domain: str) -> bool:
        return SecurityWatchdog.is_root_domain(domain)

    @staticmethod
    def _get_domain_variants(host: str) -> tuple[str, str]:
        return SecurityWatchdog.domain_variants(host)

    @staticmethod
    def _is_ip_address(host: str) -> bool:
        return SecurityWatchdog.is_ip_address(host)

    @classmethod
    def _is_url_match(cls, url: str, host: str, scheme: str, pattern: str) -> bool:
        return SecurityWatchdog.is_url_match(url, host, scheme, pattern)

    def is_url_allowed(self, url: str, is_navigation: bool = True) -> bool:
        return self.security_watchdog.is_allowed(url, is_navigation=is_navigation)

    @property
    def downloads_path(self) -> str | None:
        configured = getattr(self.browser_profile, 'downloads_path', None)
        return str(configured) if configured else self._downloads_path_native

    @downloads_path.setter
    def downloads_path(self, value: str | None) -> None:
        object.__setattr__(self, '_downloads_path_native', value)
        self.browser_profile.downloads_path = value

    @property
    def cdp_client(self) -> NoFakeCDPClient:
        return self._no_cdp_client

    async def get_or_create_cdp_session(self, *args: Any, **kwargs: Any) -> None:
        self.cdp_client._raise_error('get_or_create_cdp_session')

    async def start(self) -> None:
        if self.session_manager.context is not None:
            if not self.crash_watchdog.is_monitoring:
                from browser_use.browser.events import BrowserConnectedEvent
                await self.event_bus.dispatch(BrowserConnectedEvent(cdp_url='playwright://camoufox'))
            return
        event = self.event_bus.dispatch(BrowserStartEvent())
        await event
        await event.event_result(raise_if_any=True, raise_if_none=False)

    async def stop(self) -> None:
        await self._shutdown(force=False)

    async def _shutdown(self, *, force: bool) -> None:
        try:
            if self.session_manager.context is None:
                return
            try:
                # Stop polling before teardown so health probes cannot race it.
                await self.crash_watchdog.stop()
                storage_path = self._context_options.get('storage_state')
                if isinstance(storage_path, (str, Path)):
                    save_event = self.event_bus.dispatch(SaveStorageStateEvent(path=str(storage_path)))
                    await save_event
                    await save_event.event_result(raise_if_any=True, raise_if_none=False)
            finally:
                event = self.event_bus.dispatch(BrowserStopEvent(force=force))
                await event
                await event.event_result(raise_if_any=True, raise_if_none=False)
        finally:
            await self.event_bus.stop(clear=True, timeout=5)
            self.dom_watchdog.clear_cache()
            self.downloads_watchdog.reset()
            self.popups_watchdog.reset()
            self.security_watchdog.reset()
            self._active_init_scripts.clear()
            self.event_bus = EventBus()
            self.attach_all_watchdogs_sync()

    async def __aenter__(self) -> 'CamoufoxSession':
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.kill()

    async def ensure_context(self) -> BrowserContext:
        return await self.session_manager.ensure_context()

    async def get_current_page(self) -> Page:
        return await self.session_manager.current_page()

    async def must_get_current_page(self) -> Page:
        page = await self.get_current_page()
        if page.is_closed():
            raise RuntimeError('No current active page found.')
        return page

    async def get_pages(self) -> list[Page]:
        return list(self.session_manager.context.pages) if self.session_manager.context else []

    async def new_page(self, url: str | None = None) -> Page:
        if url and not self.is_url_allowed(url):
            self.security_watchdog.report_block(url, direct=True)
            raise ValueError(f'Tab creation with {url} blocked by security policy')
        return await self.session_manager.new_page(url)

    async def close_page(self, page: Union[Page, str]) -> None:
        await self.session_manager.close_page(page)

    async def get_current_page_url(self) -> str:
        return (await self.get_current_page()).url

    async def get_current_page_title(self) -> str:
        return await (await self.get_current_page()).title()

    async def navigate_to(self, url: str, new_tab: bool = False, timeout_ms: int = 30000) -> None:
        await self.action_watchdog.navigate(url, new_tab=new_tab, timeout_ms=timeout_ms)

    async def get_tabs(self) -> list[TabInfo]:
        return await self.session_manager.tabs()

    async def switch_tab(self, target_id: str) -> str:
        return await self.session_manager.switch(target_id)

    async def close_tab(self, target_id: str | None = None) -> None:
        await self.session_manager.close_page(target_id)

    async def get_target_id_from_tab_id(self, tab_id: str) -> str:
        page = self.session_manager.find_page(tab_id)
        if page is None:
            raise ValueError(f'No TargetID found ending in tab_id={tab_id}')
        return self.session_manager.target_id(page)

    async def get_target_id_from_url(self, url: str) -> str:
        context = await self.ensure_context()
        for page in context.pages:
            if page.url == url or url in page.url:
                return self.session_manager.target_id(page)
        raise ValueError(f'No TargetID found for url={url}')

    async def get_most_recently_opened_target_id(self) -> str:
        context = await self.ensure_context()
        if not context.pages:
            raise RuntimeError('No open page targets available.')
        return self.session_manager.target_id(context.pages[-1])

    async def cookies(self) -> list[dict[str, Any]]:
        return await self.storage_watchdog.cookies()

    async def clear_cookies(self) -> None:
        await self.storage_watchdog.clear_cookies()

    async def export_storage_state(self, output_path: str | Path | None = None) -> dict[str, Any]:
        return await self.storage_watchdog.export(output_path)

    async def _apply_storage_state(self, storage_state: str | Path | dict[str, Any]) -> tuple[int, int]:
        return await self.storage_watchdog.apply(storage_state)

    async def get_dom_state(self) -> SerializedDOMState:
        state = await self.dom_watchdog.get_state()
        self._cached_selector_map = state.selector_map
        return state

    async def get_page_info(self) -> PageInfo:
        return await self.dom_watchdog.page_info()

    async def get_browser_state_summary(
        self,
        include_screenshot: bool = True,
        cached: bool = False,
        include_recent_events: bool = False,
    ) -> BrowserStateSummary:
        event = cast(BrowserStateRequestEvent, self.event_bus.dispatch(BrowserStateRequestEvent(
            include_dom=True,
            include_screenshot=include_screenshot,
            include_recent_events=include_recent_events,
        )))
        try:
            result = await event.event_result(raise_if_none=True, raise_if_any=True)
        except TimeoutError:
            # Browser-Use contract: a timed-out capture returns non-actionable state
            # so the model can recover without selectors from an earlier page.
            state_error = (
                'Browser state capture timed out. The current DOM and screenshot are unavailable, '
                'so no element indices are safe to use. Recover with navigation, waiting, or another non-indexed action.'
            )
            self.dom_watchdog.clear_cache()
            self._cached_selector_map = {}
            page = self.session_manager.page
            url = page.url if page is not None and not page.is_closed() else 'about:blank'
            focus = self.agent_focus_target_id
            result = BrowserStateSummary(
                dom_state=SerializedDOMState(_root=None, selector_map={}),
                url=url,
                title='Browser state unavailable',
                tabs=[TabInfo(url=url, title='Browser state unavailable', target_id=focus)] if focus else [],
                screenshot=None,
                browser_errors=[state_error],
                state_error=state_error,
            )
        assert result is not None
        return result

    async def get_state_as_text(self) -> str:
        return (await self.get_browser_state_summary(include_screenshot=False)).dom_state.llm_representation()

    async def get_selector_map(self) -> dict[int, EnhancedDOMTreeNode]:
        return self.dom_watchdog.selector_map

    def update_cached_selector_map(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
        self.dom_watchdog.selector_map = selector_map
        self._cached_selector_map = selector_map

    async def get_dom_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
        return self.dom_watchdog.selector_map.get(index)

    async def get_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
        return await self.get_dom_element_by_index(index)

    async def get_index_by_id(self, element_id: str) -> int | None:
        return self.dom_watchdog.index_by_attribute('id', element_id)

    async def get_index_by_class(self, class_name: str) -> int | None:
        return self.dom_watchdog.index_by_attribute('class', class_name)

    async def get_dom_element_at_coordinates(self, x: int, y: int) -> EnhancedDOMTreeNode | None:
        return await self.dom_watchdog.element_at(x, y)

    async def get_element_coordinates(self, backend_node_id: int, cdp_session: Any = None) -> DOMRect | None:
        return await self.dom_watchdog.coordinates(backend_node_id)

    def is_file_input(self, element: Any) -> bool:
        return self.dom_watchdog.is_file_input(element)

    def find_file_input_near_element(
        self,
        node: EnhancedDOMTreeNode,
        max_height: int = 3,
        max_descendant_depth: int = 3,
    ) -> EnhancedDOMTreeNode | None:
        return self.dom_watchdog.find_file_input(node, max_height, max_descendant_depth)

    async def relocalize_node(self, node: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
        return await self.dom_watchdog.relocalize(node)

    def locator_for_node(self, node: EnhancedDOMTreeNode) -> Locator:
        return self.dom_watchdog.locator(node)

    async def take_screenshot(
        self,
        path: str | Path | None = None,
        full_page: bool = False,
        format: str = 'png',
        quality: int | None = None,
        clip: dict[str, Any] | None = None,
    ) -> bytes:
        return await self.screenshot_watchdog.capture(path, full_page, format, quality, clip)

    async def screenshot_element(
        self,
        selector: str,
        path: str | Path | None = None,
        format: str = 'png',
        quality: int | None = None,
    ) -> bytes:
        return await self.screenshot_watchdog.capture_element(selector, path, format, quality)

    async def add_highlights(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
        await self.screenshot_watchdog.add_highlights(selector_map)

    async def remove_highlights(self) -> None:
        await self.screenshot_watchdog.remove_highlights()

    async def highlight_coordinate_click(self, x: int, y: int) -> None:
        await self.screenshot_watchdog.highlight_coordinate(x, y)

    async def highlight_interaction_element(self, node: EnhancedDOMTreeNode) -> None:
        await self.screenshot_watchdog.highlight_node(node)

    async def click_action(self, index: int | Any) -> ActionResult:
        if hasattr(index, 'index'):
            index = index.index
        try:
            index = int(index)
        except (TypeError, ValueError):
            return ActionResult(error=f'Invalid index provided: {index}')
        node = await self.get_element_by_index(index)
        if node is None:
            await self.get_dom_state()
            node = await self.get_element_by_index(index)
        if node is None:
            return ActionResult(error=f'Element index {index} not found on page.')
        event = cast(ClickElementEvent, self.event_bus.dispatch(ClickElementEvent(node=node)))
        await event
        await event.event_result(raise_if_any=True, raise_if_none=False)
        return ActionResult(extracted_content=f'Clicked element {index}: <{node.node_name}> {node.node_value}'.strip())

    async def get_dropdown_options(self, node: EnhancedDOMTreeNode) -> dict[str, str]:
        return await self.action_watchdog.dropdown_options(node)

    async def select_dropdown_option(self, node: EnhancedDOMTreeNode, text: str) -> dict[str, str]:
        return await self.action_watchdog.select_dropdown(node, text)

    async def upload_file(self, node: EnhancedDOMTreeNode, file_path: str) -> None:
        await self.dom_watchdog.locator(node).set_input_files(file_path)

    @property
    def downloaded_files(self) -> list[str]:
        return list(self.downloads_watchdog.files)

    async def set_extra_headers(self, headers: dict[str, str], target_id: str | None = None) -> None:
        await (await self.ensure_context()).set_extra_http_headers(headers)

    async def connect(self, cdp_url: str | None = None) -> 'CamoufoxSession':
        if cdp_url is not None:
            raise NotImplementedError('Camoufox is launched through Playwright and cannot connect to a CDP URL.')
        await self.start()
        return self

    async def get_current_target_info(self):
        return self.get_focused_target()

    async def _get_element_bounds(self, selector: str) -> dict[str, float] | None:
        box = await (await self.get_current_page()).locator(selector).first.bounding_box()
        return dict(box) if box else None

    def _attach_ws_drop_callback(self) -> None:
        """CDP WebSocket monitoring is not applicable to Playwright-managed Camoufox."""

    async def _auto_reconnect(self, max_attempts: int = 3) -> None:
        await self.reconnect()

    async def _setup_proxy_auth(self) -> None:
        """Proxy credentials are supplied when the Playwright context is created."""

    async def _close_extension_options_pages(self) -> None:
        """Camoufox does not install Browser-Use's Chromium extensions."""

    def _cloud_session_id_from_cdp_url(self) -> None:
        return None

    async def send_demo_mode_log(
        self,
        message: str,
        level: str = 'info',
        metadata: dict[str, Any] | None = None,
    ) -> None:
        getattr(logger, level.lower(), logger.info)(f'[DemoMode] {message} {metadata or ""}'.strip())

    async def reconnect(self) -> None:
        try:
            await (await self.get_current_page()).evaluate('() => true')
        except Exception:
            await self.stop()
            await self.start()

    async def reset(self) -> None:
        await (await self.get_current_page()).goto('about:blank')
        self.dom_watchdog.clear_cache()

    async def kill(self) -> None:
        await self._shutdown(force=True)

    def get_focused_target(self):
        page = self.session_manager.page
        if page is None:
            return None
        return next((target for target in self.session_manager.targets() if target.target_id == self.agent_focus_target_id), None)

    def get_page_targets(self) -> list[Any]:
        return self.session_manager.targets()

    async def _cdp_create_new_page(self, url: str = 'about:blank', background: bool = False, new_window: bool = False) -> str:
        return self.session_manager.target_id(await self.new_page(url))

    async def _cdp_close_page(self, target_id: str) -> None:
        await self.close_tab(target_id)

    async def _cdp_get_cookies(self) -> list[dict[str, Any]]:
        return await self.cookies()

    async def _cdp_set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        await (await self.ensure_context()).add_cookies(cookies)

    async def _cdp_clear_cookies(self) -> None:
        await self.clear_cookies()

    async def _cdp_grant_permissions(self, permissions: list[str], origin: str | None = None) -> None:
        await (await self.ensure_context()).grant_permissions(permissions, origin=origin)

    async def _cdp_set_geolocation(self, latitude: float, longitude: float, accuracy: float = 100) -> None:
        await (await self.ensure_context()).set_geolocation(
            {'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy}
        )

    async def _cdp_clear_geolocation(self) -> None:
        await (await self.ensure_context()).clear_permissions()

    async def _cdp_add_init_script(self, script: str) -> str:
        identifier = f'script_{uuid.uuid4().hex[:8]}'
        self._active_init_scripts[identifier] = script
        wrapped = f"""(() => {{
            window.__camoufox_disabled_scripts ||= new Set();
            if (!window.__camoufox_disabled_scripts.has({json.dumps(identifier)})) {{ {script} }}
        }})();"""
        await (await self.ensure_context()).add_init_script(wrapped)
        return identifier

    async def _cdp_remove_init_script(self, identifier: str) -> None:
        if identifier not in self._active_init_scripts:
            raise KeyError(f'Unknown init script identifier: {identifier}')
        raise NotImplementedError('Playwright cannot remove an init script from a live BrowserContext.')

    async def _cdp_set_viewport(
        self,
        width: int,
        height: int,
        device_scale_factor: float = 1.0,
        mobile: bool = False,
        target_id: str | None = None,
    ) -> None:
        await (await self.get_current_page()).set_viewport_size({'width': width, 'height': height})

    async def _cdp_get_origins(self) -> list[dict[str, Any]]:
        return (await self.export_storage_state()).get('origins', [])

    async def _cdp_get_storage_state(self) -> dict[str, Any]:
        return await self.export_storage_state()

    async def _cdp_navigate(self, url: str, target_id: str | None = None) -> None:
        await self.navigate_to(url)

    async def _cdp_get_all_pages(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {'targetId': tab.target_id, 'type': 'page', 'url': tab.url, 'title': tab.title}
            for tab in await self.get_tabs()
        ]

    async def get_all_frames(self) -> tuple[dict[str, dict], dict[str, str]]:
        page = await self.get_current_page()
        frames = {
            f'frame-{index}': {
                'id': f'frame-{index}', 'url': frame.url, 'name': frame.name,
                'frameTargetId': self.agent_focus_target_id or tab_target_id(0),
            }
            for index, frame in enumerate(page.frames)
        }
        return frames, {}

    async def find_frame_target(
        self,
        frame_id: str,
        all_frames: dict[str, dict] | None = None,
    ) -> dict[str, Any] | None:
        if all_frames is None:
            all_frames, _ = await self.get_all_frames()
        return all_frames.get(frame_id)

    async def cdp_client_for_target(self, target_id: str) -> NoFakeCDPClient:
        return self._no_cdp_client

    async def cdp_client_for_frame(self, frame_id: str) -> NoFakeCDPClient:
        return self._no_cdp_client

    async def cdp_client_for_node(self, node: EnhancedDOMTreeNode) -> NoFakeCDPClient:
        return self._no_cdp_client

    async def is_active(self) -> bool:
        context = self.session_manager.context
        return bool(context and context.pages)

    async def get_html(self, selector: str | None = None) -> str:
        page = await self.get_current_page()
        if selector:
            result = await page.locator(selector).first.evaluate('el => el.outerHTML') if await page.locator(selector).count() else None
            return str(result) if result is not None else f'No element found for selector: {selector}'
        return await page.content()


from camoufox_browseruse.compat import apply_camoufox_mcp_compat

__all__ = ['CamoufoxSession', 'apply_camoufox_mcp_compat']

"""DOM observation, serialization, lookup, and stale-element recovery."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from browser_use.browser.events import BrowserStateRequestEvent
from browser_use.browser.views import BrowserStateSummary, NetworkRequest, PageInfo
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType, SerializedDOMState
from playwright.async_api import Locator, Page, Request

from camoufox_browseruse.dom import (
    FrameExtractionOptions,
    FrameRegistry,
    extract_interactive_elements,
    extract_page_info,
    find_best_matching_node,
    serialize_dom_state,
)
from .base import NativeWatchdog
from .screenshots import label_screenshot

logger = logging.getLogger(__name__)

# Same budget as Browser-Use: leave room inside the 30s BrowserStateRequestEvent
# timeout to return DOM-only state when screenshot capture stalls.
STATE_PARALLEL_TASK_BUDGET_SECONDS = 20.0
# Browser-Use's noise filter for its "page still loading" signal.
NOISY_REQUEST_MARKERS = (
    'doubleclick.net', 'googlesyndication.com', 'googletagmanager.com',
    'facebook.net', 'analytics', 'ads', 'tracking', 'pixel',
    'hotjar.com', 'clarity.ms', 'mixpanel.com', 'segment.com',
    'demdex.net', 'omtrdc.net', 'adobedtm.com', 'ensighten.com',
    'newrelic.com', 'nr-data.net', 'google-analytics.com',
    'connect.facebook.net', 'platform.twitter.com', 'platform.linkedin.com',
    '.cloudfront.net/image/', '.akamaized.net/image/',
    '/tracker/', '/collector/', '/beacon/', '/telemetry/', '/log/',
    '/events/', '/eventBatch', '/track.', '/metrics/',
)


class DOMWatchdog(NativeWatchdog):
    """Playwright counterpart of Browser-Use's DOMWatchdog."""

    HANDLERS = {BrowserStateRequestEvent: 'on_BrowserStateRequestEvent'}

    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.selector_map: dict[int, EnhancedDOMTreeNode] = {}
        self.selector_targets: dict[int, dict[str, Any]] = {}
        self.current_dom_state: SerializedDOMState | None = None
        self.enhanced_dom_tree: EnhancedDOMTreeNode | None = None
        self.frame_registry = FrameRegistry()
        self._inflight: dict[Request, tuple[Page, float]] = {}

    def setup_page(self, page: Page) -> None:
        # Playwright request events are the native, exact counterpart of
        # Browser-Use's performance-API probe for in-flight resources.
        page.on('request', lambda request: self._inflight.setdefault(request, (page, time.monotonic())))
        page.on('requestfinished', lambda request: self._inflight.pop(request, None))
        page.on('requestfailed', lambda request: self._inflight.pop(request, None))

    def pending_requests(self, page: Page) -> list[NetworkRequest]:
        now = time.monotonic()
        pending: list[NetworkRequest] = []
        for request, (owner, started) in list(self._inflight.items()):
            age_ms = (now - started) * 1000
            if age_ms > 10_000:
                # Long-polling/stuck requests are not "still loading" evidence.
                self._inflight.pop(request, None)
                continue
            url = request.url
            if owner is not page or url.startswith('data:') or len(url) > 500:
                continue
            if any(marker in url for marker in NOISY_REQUEST_MARKERS):
                continue
            if request.resource_type in ('image', 'font', 'media') and age_ms > 3000:
                continue
            pending.append(NetworkRequest(
                url=url,
                method=request.method,
                loading_duration_ms=round(age_ms),
                resource_type=request.resource_type,
            ))
        return pending

    def clear_cache(self) -> None:
        self.selector_map.clear()
        self.selector_targets.clear()
        self.current_dom_state = None
        self.enhanced_dom_tree = None
        self.frame_registry.clear()

    async def get_state(self) -> SerializedDOMState:
        profile = self.browser_session.browser_profile
        options = FrameExtractionOptions(
            cross_origin_iframes=bool(getattr(profile, 'cross_origin_iframes', True)),
            paint_order_filtering=bool(getattr(profile, 'paint_order_filtering', True)),
            max_iframes=int(getattr(profile, 'max_iframes', 100)),
            max_iframe_depth=int(getattr(profile, 'max_iframe_depth', 5)),
        )
        elements = await extract_interactive_elements(
            await self.browser_session.get_current_page(),
            self.frame_registry,
            options,
        )
        state, self.selector_map = serialize_dom_state(elements, self.selector_targets)
        self.current_dom_state = state
        self.enhanced_dom_tree = state._root.original_node if state._root else None
        return state

    async def _build_dom_tree_without_highlights(self, previous_state: Any = None) -> SerializedDOMState:
        return await self.get_state()

    async def page_info(self) -> PageInfo:
        return await extract_page_info(await self.browser_session.get_current_page())

    def recent_events(self, limit: int = 10) -> str:
        try:
            events = sorted(
                self.event_bus.event_history.values(),
                key=lambda event: event.event_created_at.timestamp(),
                reverse=True,
            )
            payload = []
            for event in events[:limit]:
                item = {
                    'event_type': event.event_type,
                    'timestamp': event.event_created_at.isoformat(),
                }
                for name in ('url', 'error_message', 'target_id'):
                    if hasattr(event, name):
                        item[name] = getattr(event, name)
                payload.append(item)
            return json.dumps(payload)
        except Exception:
            return '[]'

    async def on_BrowserStateRequestEvent(self, event: BrowserStateRequestEvent) -> BrowserStateSummary:
        page = await self.browser_session.get_current_page()
        started = time.monotonic()
        state_task = asyncio.create_task(self.get_state()) if event.include_dom else None
        screenshot_task = (
            asyncio.create_task(self.browser_session.take_screenshot())
            if event.include_screenshot
            else None
        )
        info_task = asyncio.create_task(self.page_info())
        title_task = asyncio.create_task(page.title())
        tabs_task = asyncio.create_task(self.browser_session.get_tabs())

        tasks = [task for task in (state_task, screenshot_task, info_task, title_task, tabs_task) if task is not None]
        screenshot_bytes = None
        try:
            await asyncio.gather(*(task for task in tasks if task is not screenshot_task))
            if screenshot_task:
                # A stalled or failed screenshot must not discard the usable DOM state.
                budget = max(0.001, STATE_PARALLEL_TASK_BUDGET_SECONDS - (time.monotonic() - started))
                try:
                    screenshot_bytes = await asyncio.wait_for(screenshot_task, timeout=budget)
                except Exception as exc:
                    logger.warning(f'State screenshot unavailable, returning DOM-only state: {type(exc).__name__}: {exc}')
        finally:
            # Cancellation or one failed observation must not orphan sibling work.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        state = state_task.result() if state_task else SerializedDOMState(_root=None, selector_map={})
        screenshot = base64.b64encode(screenshot_bytes).decode('utf-8') if screenshot_bytes else None
        info, title, tabs = info_task.result(), title_task.result(), tabs_task.result()
        profile = self.browser_session.browser_profile
        if screenshot_bytes and state.selector_map and profile.highlight_elements:
            # Upstream's agent prompt promises index labels on text-less elements in
            # the screenshot. 0.13 stopped drawing them (97420238c, for code-use mode),
            # which leaves identical tiles and icons indistinguishable to the model.
            labeled = await asyncio.to_thread(
                label_screenshot, screenshot_bytes, state.selector_map, info.viewport_width,
                profile.filter_highlight_ids,
            )
            screenshot = base64.b64encode(labeled).decode('utf-8')
        # Browser-Use interprets coordinate actions in the image space shown to
        # the LLM. Its click tool scales those coordinates back to this cached
        # CSS viewport when llm_screenshot_size is configured.
        self.browser_session._original_viewport_size = (
            info.viewport_width,
            info.viewport_height,
        )
        return BrowserStateSummary(
            dom_state=state,
            url=page.url,
            title=title,
            tabs=tabs,
            screenshot=screenshot,
            page_info=info,
            pixels_above=info.pixels_above,
            pixels_below=info.pixels_below,
            closed_popup_messages=self.browser_session.popups_watchdog.consume(),
            browser_errors=list(self.browser_session.security_watchdog.errors),
            pending_network_requests=self.pending_requests(page),
            is_pdf_viewer=page.url.lower().endswith('.pdf') or 'pdf' in page.url.lower(),
            recent_events=self.recent_events() if event.include_recent_events else None,
        )

    async def element_at(self, x: int, y: int) -> EnhancedDOMTreeNode | None:
        for node in self.selector_map.values():
            rect = node.absolute_position
            if rect and rect.x <= x <= rect.x + rect.width and rect.y <= y <= rect.y + rect.height:
                return node
        page = await self.browser_session.get_current_page()
        info = await page.evaluate(
            """([x, y]) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                return {
                    tagName: el.tagName,
                    text: (el.textContent || '').trim().slice(0, 100),
                    attributes: Object.fromEntries(Array.from(el.attributes).map(a => [a.name, a.value])),
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                };
            }""",
            [x, y],
        )
        if not info:
            return None
        rect = DOMRect(**info['rect'])
        return EnhancedDOMTreeNode(
            node_id=-1,
            backend_node_id=-1,
            node_type=NodeType.ELEMENT_NODE,
            node_name=info['tagName'],
            node_value=info['text'],
            attributes=info['attributes'],
            is_scrollable=False,
            frame_id='main',
            session_id=None,
            target_id='',
            content_document=None,
            shadow_root_type=None,
            shadow_roots=None,
            parent_node=None,
            children_nodes=None,
            ax_node=None,
            snapshot_node=None,
            is_visible=True,
            absolute_position=rect,
        )

    def is_file_input(self, node: Any) -> bool:
        return bool(
            hasattr(node, 'node_name')
            and node.node_name.upper() == 'INPUT'
            and getattr(node, 'attributes', None)
            and node.attributes.get('type', '').lower() == 'file'
        )

    def find_file_input(
        self,
        node: EnhancedDOMTreeNode,
        max_height: int = 3,
        max_descendant_depth: int = 3,
    ) -> EnhancedDOMTreeNode | None:
        def descend(item: EnhancedDOMTreeNode, depth: int) -> EnhancedDOMTreeNode | None:
            if depth < 0:
                return None
            if self.is_file_input(item):
                return item
            for child in item.children_nodes or []:
                if result := descend(child, depth - 1):
                    return result
            return None

        current: EnhancedDOMTreeNode | None = node
        for _ in range(max_height + 1):
            if current is None:
                break
            if result := descend(current, max_descendant_depth):
                return result
            if current.parent_node:
                for sibling in current.parent_node.children_nodes or []:
                    if sibling is current:
                        continue
                    if result := descend(sibling, max_descendant_depth):
                        return result
            current = current.parent_node
        return None

    async def relocalize(self, node: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
        target = self.selector_targets.get(node.node_id)
        if target:
            original = (node.attributes or {}).get('data-browser-use-camoufox-selector')
            if not original or original == target.get('selector'):
                try:
                    locator = self.locator(node)
                    if await locator.count() == 1 and await locator.is_visible():
                        identity = {key: value for key in ('id', 'data-testid', 'name', 'type', 'role')
                                    if (value := (node.attributes or {}).get(key))}
                        if not await locator.evaluate(
                            '(el, identity) => Object.entries(identity).every(([key, value]) => el.getAttribute(key) === value)', identity,
                        ):
                            raise RuntimeError('The observed locator now belongs to a different control.')
                        expected = (node.node_value or '').strip()
                        actual = (await locator.text_content(timeout=1000) or '').strip()
                        if not expected or expected in actual or actual in expected:
                            return node
                except Exception:
                    pass
        await self.get_state()
        match = find_best_matching_node(node, list(self.selector_map.values()))
        if match is None:
            raise RuntimeError(f'Element {node.node_id} is stale and could not be relocalized without ambiguity.')
        return match

    def locator(self, node: EnhancedDOMTreeNode) -> Locator:
        page = self.browser_session.session_manager.page
        if page is None:
            raise RuntimeError('Camoufox page is not active.')
        target = self.selector_targets.get(node.node_id, {})
        attrs = node.attributes or {}
        selector = target.get('selector') or attrs.get('data-browser-use-camoufox-selector') or '*'
        ordinal = int(target.get('ordinal', attrs.get('data-browser-use-camoufox-ordinal', 0)))
        frame_id = target.get('frame_id') or attrs.get('data-browser-use-camoufox-frame', 'main')
        frame = self.frame_registry.frames_by_id.get(str(frame_id))
        if frame is None or frame.is_detached():
            raise RuntimeError(f'DOM target frame {frame_id!r} is no longer attached.')
        return frame.locator(selector).nth(ordinal)

    async def coordinates(self, node_id: int) -> DOMRect | None:
        for node in self.selector_map.values():
            if node.backend_node_id == node_id or node.node_id == node_id:
                return node.absolute_position
        return None

    def index_by_attribute(self, name: str, value: str) -> int | None:
        for index, node in self.selector_map.items():
            actual = (node.attributes or {}).get(name)
            if actual == value or (name == 'class' and value in (actual or '').split()):
                return index
        return None

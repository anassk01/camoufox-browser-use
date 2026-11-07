"""Default browser actions implemented with native Playwright calls."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from browser_use.browser.events import (
    ClickCoordinateEvent,
    ClickElementEvent,
    CloseTabEvent,
    GetDropdownOptionsEvent,
    GoBackEvent,
    GoForwardEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    RefreshEvent,
    ScrollEvent,
    ScrollToTextEvent,
    SelectDropdownOptionEvent,
    SendKeysEvent,
    SwitchTabEvent,
    TypeTextEvent,
    UploadFileEvent,
    WaitEvent,
)
from browser_use.dom.views import EnhancedDOMTreeNode

from camoufox_browseruse.interactions import InteractionTransaction
from .base import NativeWatchdog


class ActionWatchdog(NativeWatchdog):
    """Playwright counterpart of Browser-Use's DefaultActionWatchdog."""

    HANDLERS = {
        NavigateToUrlEvent: 'on_NavigateToUrlEvent',
        GoBackEvent: 'on_GoBackEvent',
        GoForwardEvent: 'on_GoForwardEvent',
        RefreshEvent: 'on_RefreshEvent',
        WaitEvent: 'on_WaitEvent',
        SwitchTabEvent: 'on_SwitchTabEvent',
        CloseTabEvent: 'on_CloseTabEvent',
        ClickElementEvent: 'on_ClickElementEvent',
        ClickCoordinateEvent: 'on_ClickCoordinateEvent',
        TypeTextEvent: 'on_TypeTextEvent',
        ScrollEvent: 'on_ScrollEvent',
        ScrollToTextEvent: 'on_ScrollToTextEvent',
        SendKeysEvent: 'on_SendKeysEvent',
        GetDropdownOptionsEvent: 'on_GetDropdownOptionsEvent',
        SelectDropdownOptionEvent: 'on_SelectDropdownOptionEvent',
        UploadFileEvent: 'on_UploadFileEvent',
    }

    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.interactions = InteractionTransaction(browser_session)

    async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> dict[str, str]:
        await self.navigate(
            event.url,
            new_tab=event.new_tab,
            timeout_ms=event.timeout_ms or 30000,
            wait_until=event.wait_until,
        )
        return {'url': event.url}

    async def navigate(
        self,
        url: str,
        *,
        new_tab: bool = False,
        timeout_ms: int = 30000,
        wait_until: str = 'domcontentloaded',
    ) -> None:
        session = self.browser_session
        if not session.security_watchdog.is_allowed(url):
            session.security_watchdog.report_block(url, direct=True)
            raise ValueError(f'Navigation to {url} blocked by security policy')
        page = await session.get_current_page()
        if new_tab and page.url not in {'about:blank', 'about:newtab', 'about:home'}:
            page = await session.session_manager.new_page()
        target_id = session.session_manager.target_id(page)
        await self.event_bus.dispatch(NavigationStartedEvent(target_id=target_id, url=url))
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if not session.security_watchdog.is_allowed(page.url):
                blocked_url = page.url
                session.security_watchdog.report_block(blocked_url)
                await page.goto('about:blank')
                raise ValueError(f'Navigation to {blocked_url} blocked by security policy')
            await self.event_bus.dispatch(NavigationCompleteEvent(
                target_id=target_id,
                url=page.url,
                status=response.status if response else None,
                loading_status='complete',
            ))
        except Exception as exc:
            message = str(exc).splitlines()[0]
            await self.event_bus.dispatch(NavigationCompleteEvent(
                target_id=target_id,
                url=page.url or url,
                error_message=message,
                loading_status='failed',
            ))
            if isinstance(exc, ValueError):
                raise
            raise RuntimeError(f'Navigation failed for {url}: {message}') from exc

    async def on_GoBackEvent(self, event: GoBackEvent) -> dict[str, bool]:
        try:
            await (await self.browser_session.get_current_page()).go_back(wait_until='domcontentloaded', timeout=5000)
        except Exception:
            pass
        return {'back': True}

    async def on_GoForwardEvent(self, event: GoForwardEvent) -> dict[str, bool]:
        try:
            await (await self.browser_session.get_current_page()).go_forward(wait_until='domcontentloaded', timeout=5000)
        except Exception:
            pass
        return {'forward': True}

    async def on_RefreshEvent(self, event: RefreshEvent) -> dict[str, bool]:
        try:
            await (await self.browser_session.get_current_page()).reload(wait_until='domcontentloaded', timeout=5000)
        except Exception:
            pass
        return {'reloaded': True}

    async def on_WaitEvent(self, event: WaitEvent) -> dict[str, bool]:
        seconds = max(0.0, float(event.seconds))
        await asyncio.sleep(min(seconds, max(0.0, float(event.max_seconds))))
        return {'waited': True}

    async def on_SwitchTabEvent(self, event: SwitchTabEvent) -> str:
        return await self.browser_session.session_manager.switch(event.target_id)

    async def on_CloseTabEvent(self, event: CloseTabEvent) -> dict[str, bool]:
        await self.browser_session.session_manager.close_page(event.target_id)
        return {'closed': True}

    async def on_ClickElementEvent(self, event: ClickElementEvent) -> dict[str, Any]:
        node = event.node
        attributes = node.attributes or {}
        if (node.node_name or '').lower() == 'select':
            return {
                'validation_error': (
                    f'Cannot click on <select> elements. '
                    f'Use dropdown_options(index={node.node_id}) action instead.'
                )
            }
        if attributes.get('type') == 'file':
            return {
                'validation_error': (
                    f'Cannot click on file input element (index={node.node_id}). '
                    'File uploads must be handled using upload_file action.'
                )
            }
        if attributes.get('data-browser-use-camoufox-disabled') == 'true':
            raise RuntimeError(f'Element {node.node_id} is disabled.')
        button = getattr(event, 'button', 'left') or 'left'
        tabs_before = (
            {tab.target_id for tab in await self.browser_session.get_tabs()}
            if attributes.get('target', '').lower() == '_blank'
            else None
        )
        result = await self.interactions.click(node, button=button)
        metadata: dict[str, Any] = {
            'clicked': True,
            'node_id': result.node.node_id,
            'attempts': result.attempts,
            'strategy': result.strategy,
        }
        # Firefox may emit the BrowserContext "page" event shortly after the
        # trusted click returns.  Browser-Use checks for new tabs immediately
        # after this handler, so wait only for controls that explicitly declare
        # target=_blank and surface the stable target ID in action metadata.
        if tabs_before is not None:
            for _ in range(60):
                tabs_after = await self.browser_session.get_tabs()
                opened = [tab.target_id for tab in tabs_after if tab.target_id not in tabs_before]
                if opened:
                    metadata['new_target_id'] = opened[-1]
                    break
                await asyncio.sleep(0.05)
        return metadata

    async def on_ClickCoordinateEvent(self, event: ClickCoordinateEvent) -> dict[str, int | str]:
        x, y = int(event.coordinate_x), int(event.coordinate_y)
        return await self.interactions.click_at(
            x,
            y,
            button=getattr(event, 'button', 'left') or 'left',
            force=bool(getattr(event, 'force', False)),
        )

    async def drag_coordinates(
        self,
        source_x: int,
        source_y: int,
        target_x: int,
        target_y: int,
    ) -> dict[str, Any]:
        return await self.interactions.drag_at(
            source_x,
            source_y,
            target_x,
            target_y,
        )

    async def on_TypeTextEvent(self, event: TypeTextEvent) -> dict[str, Any]:
        node = event.node
        attributes = node.attributes or {}
        if attributes.get('type') == 'file':
            raise RuntimeError('File inputs cannot be edited directly. Use upload_file.')
        if attributes.get('data-browser-use-camoufox-disabled') == 'true':
            raise RuntimeError(f'Element {node.node_id} is disabled.')
        clear = getattr(event, 'clear', False)
        result = await self.interactions.type_text(node, event.text, clear=clear)
        return {
            'actual_value': event.text,
            'node_id': result.node.node_id,
            'attempts': result.attempts,
            'strategy': result.strategy,
        }

    async def scroll_metrics(
        self,
        node: EnhancedDOMTreeNode | None = None,
        direction: str = 'down',
    ) -> dict[str, float | str | None]:
        return await self.interactions.scroll_metrics(node, direction)

    async def on_ScrollEvent(self, event: ScrollEvent) -> dict[str, Any]:
        direction = getattr(event, 'direction', 'down')
        amount = max(0, int(getattr(event, 'amount', 500)))
        return await self.interactions.scroll(event.node, direction=direction, amount=amount)

    async def on_ScrollToTextEvent(self, event: ScrollToTextEvent) -> dict[str, bool | str]:
        return await self.interactions.scroll_to_text(event.text)

    async def on_SendKeysEvent(self, event: SendKeysEvent) -> dict[str, str]:
        if getattr(event, 'target_id', None) is not None:
            await self.browser_session.session_manager.switch(event.target_id)
        return await self.interactions.send_keys(event.keys)

    async def on_GetDropdownOptionsEvent(self, event: GetDropdownOptionsEvent) -> dict[str, str]:
        return await self.dropdown_options(event.node)

    async def on_SelectDropdownOptionEvent(self, event: SelectDropdownOptionEvent) -> dict[str, str]:
        return await self.select_dropdown(event.node, getattr(event, 'text', ''))

    async def on_UploadFileEvent(self, event: UploadFileEvent) -> dict[str, bool]:
        path = getattr(event, 'file_path', None) or getattr(event, 'path', None)
        if not path:
            raise ValueError('No file path provided.')
        await self.browser_session.dom_watchdog.locator(event.node).set_input_files(str(path))
        return {'uploaded': True}

    async def dropdown_options(self, node: EnhancedDOMTreeNode) -> dict[str, str]:
        node = await self.browser_session.dom_watchdog.relocalize(node)
        kind = 'select'
        if node.node_name.lower() != 'select':
            from camoufox_browseruse.dropdowns import custom_dropdown, custom_options
            _, popup = await custom_dropdown(self.browser_session, node)
            options = await custom_options(popup)
            kind = 'aria'
        else:
            options = await self.browser_session.dom_watchdog.locator(node).evaluate(
                """select => Array.from(select.options || []).map((option, index) => ({
                    text: (option.text || option.label || '').trim(), value: option.value,
                    index, selected: option.selected
                }))"""
            )
        node_id = node.backend_node_id or node.node_id
        formatted = '\n'.join(
            f"{item['index']}: text={json.dumps(item['text'])}, value={json.dumps(item['value'])}"
            + (' (selected)' if item['selected'] else '')
            + (' (disabled)' if item.get('disabled') else '')
            for item in options
        )
        message = (
            f'Found {kind} dropdown (Index: {node_id}):\n{formatted}' if options
            else f'No options found in dropdown at index {node_id}'
        )
        return {
            'type': kind, 'options': json.dumps(options), 'element_info': f'Index: {node_id}, Type: {kind}',
            'source': 'target', 'formatted_options': formatted, 'message': message,
            'short_term_memory': message, 'long_term_memory': message, 'backend_node_id': str(node_id),
        }

    async def select_dropdown(self, node: EnhancedDOMTreeNode, text: str) -> dict[str, str]:
        node = await self.browser_session.dom_watchdog.relocalize(node)
        locator = self.browser_session.dom_watchdog.locator(node)
        try:
            if node.node_name.lower() != 'select':
                from camoufox_browseruse.dropdowns import select_custom_option
                await select_custom_option(self.browser_session, node, text)
                values = [text]
            else:
                values = await locator.select_option(label=text, timeout=2000)
                if not values:
                    values = await locator.select_option(value=text, timeout=2000)
        except Exception as exc:
            error = f'Failed to select option {text!r}: {exc}'
            return {'success': 'false', 'error': error, 'short_term_memory': error, 'long_term_memory': error}
        node_id = node.backend_node_id or node.node_id
        if values:
            message = f'Selected option: {text}'
            return {'success': 'true', 'message': message, 'short_term_memory': message, 'long_term_memory': message}
        error = f"Failed to select option '{text}' in dropdown index {node_id}"
        return {'success': 'false', 'error': error, 'short_term_memory': error, 'long_term_memory': error}

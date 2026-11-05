"""Generic, bounded interaction transactions for dynamic web applications."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from browser_use.dom.views import EnhancedDOMTreeNode

from camoufox_browseruse.helpers import classify_keyboard_input
from camoufox_browseruse.native_input import FirefoxNativeInput, NativeInputNotDispatched

if TYPE_CHECKING:
    from playwright.async_api import Locator

    from camoufox_browseruse.session import CamoufoxSession


@dataclass(frozen=True)
class InteractionResult:
    node: EnhancedDOMTreeNode
    attempts: int
    strategy: str


_SCROLL_TARGET_METRICS_JS = r"""(element, axis) => {
    const parentOf = node => node.parentElement
        || (node.getRootNode && node.getRootNode().host)
        || null;
    const canScroll = node => {
        const style = getComputedStyle(node);
        return axis === 'y'
            ? !['visible', 'clip'].includes(style.overflowY)
                && node.scrollHeight > node.clientHeight + 1
            : !['visible', 'clip'].includes(style.overflowX)
                && node.scrollWidth > node.clientWidth + 1;
    };
    let target = element;
    while (target && target !== document.body && target !== document.documentElement) {
        if (canScroll(target)) break;
        target = parentOf(target);
    }
    if (!target || target === document.body || target === document.documentElement) {
        target = document.scrollingElement || document.documentElement;
    }
    const rect = target.getBoundingClientRect();
    const left = Math.max(1, rect.left);
    const right = Math.min(window.innerWidth - 1, rect.right);
    const top = Math.max(1, rect.top);
    const bottom = Math.min(window.innerHeight - 1, rect.bottom);
    return {
        target: target === (document.scrollingElement || document.documentElement)
            ? 'page'
            : target === element ? 'element' : 'ancestor',
        x: target.scrollLeft,
        y: target.scrollTop,
        max_x: Math.max(0, target.scrollWidth - target.clientWidth),
        max_y: Math.max(0, target.scrollHeight - target.clientHeight),
        client_width: target.clientWidth,
        client_height: target.clientHeight,
        scroll_width: target.scrollWidth,
        scroll_height: target.scrollHeight,
        wheel_x: right > left ? Math.round((left + right) / 2) : null,
        wheel_y: bottom > top ? Math.round((top + bottom) / 2) : null
    };
}"""


class InteractionTransaction:
    """Observe, act, verify, and recover without site-specific selectors."""

    def __init__(self, session: 'CamoufoxSession', *, max_attempts: int = 3) -> None:
        self.session = session
        self.max_attempts = max(1, max_attempts)
        self._lock = asyncio.Lock()
        self.input = FirefoxNativeInput(session)

    async def _refresh_after_failure(self, node: EnhancedDOMTreeNode, attempt: int) -> None:
        await asyncio.sleep(0.15 * attempt)
        await self.session.dom_watchdog.get_state()
        # Fail closed when identity is ambiguous. relocalize() applies the
        # semantic identity checks shared by all Browser-Use actions.
        await self.session.dom_watchdog.relocalize(node)

    async def _resolve(self, node: EnhancedDOMTreeNode) -> tuple[EnhancedDOMTreeNode, 'Locator']:
        resolved = await self.session.dom_watchdog.relocalize(node)
        return resolved, self.session.dom_watchdog.locator(resolved)

    @staticmethod
    async def _scroll(locator: 'Locator') -> None:
        fully_in_view = await locator.evaluate(
            """el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0
                    && r.bottom <= window.innerHeight && r.right <= window.innerWidth;
            }""",
            timeout=2000,
        )
        if not fully_in_view:
            # Playwright performs the minimum necessary scroll. Unconditionally
            # calling scrollIntoView dismisses many open menus and popovers.
            await locator.scroll_into_view_if_needed(timeout=3000)

    @staticmethod
    async def _control_state(locator: 'Locator') -> dict[str, Any]:
        return await locator.evaluate(
            r"""el => {
                const labelledBy = (el.getAttribute('aria-labelledby') || '')
                    .trim().split(/\s+/).filter(Boolean);
                const root = el.getRootNode();
                const labels = labelledBy.map(id =>
                    root.getElementById?.(id) || el.ownerDocument.getElementById(id)
                ).filter(Boolean);
                return {
                    checked: typeof el.checked === 'boolean' ? el.checked : null,
                    ariaChecked: el.getAttribute('aria-checked'),
                    ariaExpanded: el.getAttribute('aria-expanded'),
                    ariaBusy: el.getAttribute('aria-busy'),
                    disabled: Boolean(el.disabled)
                        || el.getAttribute('aria-disabled') === 'true',
                    className: typeof el.className === 'string'
                        ? el.className
                        : (el.getAttribute('class') || ''),
                    labelText: labels.map(label => label.textContent || '').join(' ').trim()
                        || el.getAttribute('aria-label')
                        || ''
                };
            }""",
            timeout=1500,
        )

    @staticmethod
    async def _wait_for_click_ready(locator: 'Locator', timeout: float = 3.5) -> None:
        """Poll actionability without invoking Camoufox's input-producing click path."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        previous_rect: dict[str, float] | None = None
        stable_samples = 0
        last_reason = 'target did not become actionable'
        while loop.time() < deadline:
            try:
                state = await locator.evaluate(
                    """el => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        const x = rect.left + rect.width / 2;
                        const y = rect.top + rect.height / 2;
                        let hit = document.elementFromPoint(x, y);
                        while (hit && hit.shadowRoot) {
                            const deeper = typeof hit.shadowRoot.elementFromPoint === 'function'
                                ? hit.shadowRoot.elementFromPoint(x, y)
                                : hit.shadowRoot.elementsFromPoint?.(x, y)?.[0];
                            if (!deeper || deeper === hit) break;
                            hit = deeper;
                        }
                        return {
                            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                            visible: rect.width > 0 && rect.height > 0
                                && style.display !== 'none' && style.visibility !== 'hidden'
                                && style.opacity !== '0',
                            enabled: !el.matches(':disabled')
                                && el.getAttribute('aria-disabled') !== 'true',
                            receivesEvents: Boolean(hit && (hit === el || el.contains(hit)))
                        };
                    }""",
                    timeout=1000,
                )
                if not state['visible']:
                    last_reason = 'target is not visible'
                elif not state['enabled']:
                    last_reason = 'target is disabled'
                elif not state['receivesEvents']:
                    last_reason = 'target is covered by another element'
                else:
                    rect = state['rect']
                    if previous_rect is not None and all(
                        abs(float(rect[key]) - float(previous_rect[key])) < 0.5
                        for key in ('x', 'y', 'width', 'height')
                    ):
                        stable_samples += 1
                    else:
                        stable_samples = 0
                    previous_rect = rect
                    if stable_samples >= 1:
                        return
            except Exception as exc:
                last_reason = str(exc).splitlines()[0]
                previous_rect = None
                stable_samples = 0
            await asyncio.sleep(0.05)
        raise TimeoutError(last_reason)

    async def _verify_toggle(
        self,
        locator: 'Locator',
        before: dict[str, Any],
        node: EnhancedDOMTreeNode,
    ) -> None:
        attrs = node.attributes or {}
        input_type = attrs.get('type', '').lower()
        role = attrs.get('role', '').lower()
        if input_type not in {'checkbox', 'radio'} and role not in {'checkbox', 'radio', 'switch'}:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.5
        current_locator = locator
        while True:
            try:
                after = await self._control_state(current_locator)
                selected = (
                    after['checked'] is True
                    or after['ariaChecked'] == 'true'
                )
                if input_type == 'radio' or role == 'radio':
                    if selected:
                        return
                else:
                    native_changed = (
                        before['checked'] is not None
                        and after['checked'] != before['checked']
                    )
                    aria_changed = (
                        before['ariaChecked'] is not None
                        and after['ariaChecked'] != before['ariaChecked']
                    )
                    if native_changed or aria_changed:
                        return

                # ARIA widgets often acknowledge the activation with a loading
                # state while a remote operation decides their final value.
                # That observable transition proves the one click was handled;
                # waiting for the remote business outcome belongs to the next
                # observation, not to input delivery.
                if role and any(
                    after[key] != before[key]
                    for key in ('ariaBusy', 'disabled', 'className', 'labelText')
                ):
                    return
            except Exception:
                try:
                    _, current_locator = await self._resolve(node)
                except Exception:
                    pass
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.05)

        kind = 'Radio' if input_type == 'radio' or role == 'radio' else 'Toggle'
        raise RuntimeError(
            f'{kind} click completed but the control did not change '
            'or acknowledge activation.'
        )

    async def _option_context(self) -> dict[str, Any]:
        page = await self.session.get_current_page()
        return {
            'url': page.url,
            'controls': await page.evaluate(
                r"""() => Array.from(document.querySelectorAll('[role="combobox"]'))
                    .filter(el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none'
                            && s.visibility !== 'hidden' && s.opacity !== '0';
                    })
                    .map(el => ({
                        text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
                        value: 'value' in el ? String(el.value || '') : ''
                    }))"""
            ),
        }

    async def _verify_option(self, node: EnhancedDOMTreeNode, before: dict[str, Any] | None) -> None:
        attrs = node.attributes or {}
        if attrs.get('role', '').lower() != 'option':
            return
        expected = ' '.join((node.node_value or '').split())
        if not expected:
            return
        page = await self.session.get_current_page()
        for _ in range(20):
            selected = await page.evaluate(
                r"""expected => {
                    const norm = value => (value || '').replace(/\s+/g, ' ').trim();
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none'
                            && s.visibility !== 'hidden' && s.opacity !== '0';
                    };
                    const options = Array.from(document.querySelectorAll('[role="option"]'));
                    if (options.some(el => visible(el) && norm(el.textContent) === expected
                            && el.getAttribute('aria-selected') === 'true')) return true;
                    const combos = Array.from(document.querySelectorAll('[role="combobox"]'));
                    return combos.some(el => visible(el) && norm(el.textContent) === expected);
                }""",
                expected,
            )
            if selected:
                return
            if before is not None:
                after = await self._option_context()
                if after['url'] != before['url'] or after['controls'] != before['controls']:
                    return
            await asyncio.sleep(0.1)
        raise RuntimeError(f'Option click completed but {expected!r} was not selected.')

    async def click(self, node: EnhancedDOMTreeNode, *, button: str = 'left') -> InteractionResult:
        """Prepare with retries, then send at most one trusted activation."""
        async with self._lock:
            errors: list[str] = []
            click_attempts = min(self.max_attempts, 2)
            resolved = None
            locator = None
            before = None
            option_before = None
            for attempt in range(1, click_attempts + 1):
                try:
                    resolved, locator = await self._resolve(node)
                    await self._scroll(locator)
                    before = await self._control_state(locator)
                    option_before = (
                        await self._option_context()
                        if (resolved.attributes or {}).get('role', '').lower() == 'option'
                        else None
                    )
                    if before['disabled']:
                        raise RuntimeError('Target became disabled before the click.')
                    # Camoufox humanizes locator clicks, including Playwright's
                    # trial mode. Poll actionability ourselves so preparation
                    # cannot enter an input-producing path.
                    await self._wait_for_click_ready(locator)
                    break
                except Exception as exc:
                    errors.append(str(exc).splitlines()[0])
                    if attempt == click_attempts:
                        detail = '; '.join(dict.fromkeys(error for error in errors if error))
                        raise RuntimeError(
                            f'Interaction failed before input after {click_attempts} observations: {detail}'
                        ) from exc
                    try:
                        await self._refresh_after_failure(node, attempt)
                    except Exception as refresh_exc:
                        errors.append(str(refresh_exc).splitlines()[0])

            assert resolved is not None and locator is not None and before is not None
            try:
                # After this call begins, repeating the activation is unsafe: a
                # handler may already have submitted, purchased, or navigated.
                strategy = await self.input.click(locator, button=button)
                await self._verify_toggle(locator, before, resolved)
                await self._verify_option(resolved, option_before)
            except NativeInputNotDispatched as exc:
                raise RuntimeError(f'Click failed before input: {exc}') from exc
            except Exception as exc:
                message = str(exc).splitlines()[0]
                raise RuntimeError(
                    f'Click input was sent at most once; outcome is uncertain: {message}'
                ) from exc
            return InteractionResult(resolved, attempt, strategy)

    @staticmethod
    async def _text_value(locator: 'Locator') -> str:
        return str(await locator.evaluate(
            """el => el.isContentEditable
                ? (el.innerText || el.textContent || '')
                : ('value' in el ? String(el.value || '') : (el.textContent || ''))""",
            timeout=1500,
        ))

    @staticmethod
    def _value_matches(actual: str, expected: str, clear: bool) -> bool:
        return actual == expected if clear else actual.endswith(expected)

    async def _visible_control_has_value(
        self,
        node: EnhancedDOMTreeNode,
        expected: str,
        clear: bool,
    ) -> bool:
        identity = {
            name: value
            for name in ('aria-label', 'placeholder', 'name', 'type', 'role')
            if (value := (node.attributes or {}).get(name))
        }
        page = await self.session.get_current_page()
        return bool(await page.evaluate(
            r"""({identity, expected, clear}) => {
                const candidates = Array.from(document.querySelectorAll(
                    'input, textarea, [contenteditable=""], [contenteditable="true"], [role="textbox"]'
                ));
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none'
                        && s.visibility !== 'hidden' && s.opacity !== '0';
                };
                return candidates.some(el => {
                    if (!visible(el)) return false;
                    const keys = Object.keys(identity);
                    if (keys.length && !keys.some(key => el.getAttribute(key) === identity[key])) return false;
                    const actual = el.isContentEditable
                        ? (el.innerText || el.textContent || '')
                        : ('value' in el ? String(el.value || '') : (el.textContent || ''));
                    return clear ? actual === expected : actual.endsWith(expected);
                });
            }""",
            {'identity': identity, 'expected': expected, 'clear': clear},
        ))

    async def _wait_for_text_value(
        self,
        node: EnhancedDOMTreeNode,
        expected: str,
        clear: bool,
        *,
        timeout: float = 1.5,
    ) -> str:
        """Poll a controlled input's committed value without replaying input."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        actual = ''
        while True:
            try:
                _, locator = await self._resolve(node)
                actual = await self._text_value(locator)
                if self._value_matches(actual, expected, clear):
                    return actual
            except Exception:
                # A framework may replace the control again while committing.
                # Keep observing through the same semantic identity until the
                # bounded verification deadline.
                pass
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.05)

        if await self._visible_control_has_value(node, expected, clear):
            return expected
        raise RuntimeError(
            f'Input verification failed (expected {expected!r}, observed {actual!r}).'
        )

    async def type_text(
        self,
        node: EnhancedDOMTreeNode,
        text: str,
        *,
        clear: bool,
    ) -> InteractionResult:
        """Prepare with retries, then edit and verify without blind replay."""
        async with self._lock:
            errors: list[str] = []
            resolved = None
            locator = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    resolved, locator = await self._resolve(node)
                    await self._scroll(locator)
                    # Typing does not require a pointer click. Focusing directly
                    # avoids unrelated hit-target failures while still firing the
                    # framework focus lifecycle that may replace the control.
                    await locator.focus(timeout=2500)

                    # Frameworks commonly replace an input on focus. Resolve it
                    # again before editing instead of retaining a stale handle.
                    resolved, locator = await self._resolve(node)
                    if not await locator.is_editable(timeout=1500):
                        raise RuntimeError('Target is not editable before text input.')
                    break
                except Exception as exc:
                    errors.append(str(exc).splitlines()[0])
                    if attempt == self.max_attempts:
                        detail = '; '.join(dict.fromkeys(error for error in errors if error))
                        raise RuntimeError(
                            f'Text interaction failed before input after {self.max_attempts} observations: {detail}'
                        ) from exc
                    try:
                        await self._refresh_after_failure(node, attempt)
                    except Exception as refresh_exc:
                        errors.append(str(refresh_exc).splitlines()[0])

            assert resolved is not None and locator is not None
            attributes = resolved.attributes or {}
            input_type = attributes.get('type', '').lower()
            direct_fill = (
                resolved.node_name.lower() == 'input'
                and (
                    input_type in {'date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'}
                    or any(
                        marker in attributes.get('class', '').lower()
                        for marker in ('datepicker', 'daterangepicker', 'datetimepicker', 'bootstrap-datepicker')
                    )
                    or any(name in attributes for name in ('data-datepicker', 'data-date-format', 'data-provide'))
                )
            )
            try:
                strategy = await self.input.type_text(
                    locator,
                    text,
                    clear=clear,
                    direct_fill=direct_fill,
                )
                await self._wait_for_text_value(node, text, clear)
            except Exception as exc:
                message = str(exc).splitlines()[0]
                raise RuntimeError(
                    f'Text input was sent at most once; outcome is uncertain: {message}'
                ) from exc
            return InteractionResult(resolved, attempt, strategy)

    async def click_at(
        self,
        x: int,
        y: int,
        *,
        button: str = 'left',
        force: bool = False,
    ) -> dict[str, int | str]:
        """Validate and perform one coordinate click under the input lock."""
        async with self._lock:
            page = await self.session.get_current_page()
            viewport = page.viewport_size
            if x < 0 or y < 0 or (viewport and (x >= viewport['width'] or y >= viewport['height'])):
                raise RuntimeError(f'Click coordinates ({x}, {y}) are outside the viewport.')
            target = await page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return null;
                    return {
                        tag: (el.tagName || '').toLowerCase(),
                        type: (el.getAttribute('type') || '').toLowerCase()
                    };
                }""",
                [x, y],
            )
            if not force:
                if target is None:
                    raise RuntimeError(f'No element at ({x}, {y}).')
                if target['type'] == 'file':
                    raise RuntimeError('File inputs cannot be clicked directly. Use upload_file.')
                if target['tag'] == 'select':
                    raise RuntimeError('Select controls require select_dropdown_option.')
            try:
                await self.input.click_at(x, y, button=button)
            except NativeInputNotDispatched as exc:
                raise RuntimeError(f'Coordinate click failed before input: {exc}') from exc
            except Exception as exc:
                message = str(exc).splitlines()[0]
                raise RuntimeError(
                    f'Coordinate click was sent at most once; outcome is uncertain: {message}'
                ) from exc
            return {'x': x, 'y': y, 'strategy': 'trusted-coordinate-click'}

    async def drag_at(
        self,
        source_x: int,
        source_y: int,
        target_x: int,
        target_y: int,
    ) -> dict[str, Any]:
        """Validate and perform one coordinate drag under the input lock."""
        async with self._lock:
            page = await self.session.get_current_page()
            viewport = page.viewport_size
            points = ((source_x, source_y), (target_x, target_y))
            for x, y in points:
                if x < 0 or y < 0 or (
                    viewport and (x >= viewport['width'] or y >= viewport['height'])
                ):
                    raise RuntimeError(f'Drag coordinates ({x}, {y}) are outside the viewport.')
            targets = await page.evaluate(
                """points => points.map(([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    return el ? (el.tagName || '').toLowerCase() : null;
                })""",
                [list(point) for point in points],
            )
            if not targets[0]:
                raise RuntimeError(f'No drag source at ({source_x}, {source_y}).')
            if not targets[1]:
                raise RuntimeError(f'No drag target at ({target_x}, {target_y}).')

            probe = None
            try:
                probe = await page.evaluate_handle(
                    """points => {
                        const signature = element => element ? {
                            tag: (element.tagName || '').toLowerCase(),
                            className: typeof element.className === 'string' ? element.className : '',
                            style: element.getAttribute?.('style') || ''
                        } : null;
                        const source = document.elementFromPoint(...points[0]);
                        const target = document.elementFromPoint(...points[1]);
                        let root = source;
                        while (root && target && root !== target && !root.contains(target)) {
                            root = root.parentElement || root.getRootNode?.().host || null;
                        }
                        const state = {
                            changes: 0,
                            sourceBefore: signature(source),
                            targetBefore: signature(target)
                        };
                        state.observer = new MutationObserver(records => state.changes += records.length);
                        state.observer.observe(root || document.documentElement, {
                            subtree: true,
                            childList: true,
                            attributes: true,
                            characterData: true
                        });
                        return state;
                    }""",
                    [[source_x, source_y], [target_x, target_y]],
                )
            except Exception:
                # Outcome observation is diagnostic and must not prevent native
                # delivery when a page blocks MutationObserver instrumentation.
                probe = None

            async def dispose_probe() -> None:
                nonlocal probe
                if probe is None:
                    return
                try:
                    await probe.evaluate('state => state.observer.disconnect()')
                except Exception:
                    pass
                try:
                    await probe.dispose()
                except Exception:
                    pass
                probe = None

            try:
                await self.input.drag_at(source_x, source_y, target_x, target_y)
            except NativeInputNotDispatched as exc:
                await dispose_probe()
                raise RuntimeError(f'Coordinate drag failed before input: {exc}') from exc
            except asyncio.CancelledError:
                await dispose_probe()
                raise
            except Exception as exc:
                await dispose_probe()
                message = str(exc).splitlines()[0]
                raise RuntimeError(
                    f'Coordinate drag was sent at most once; outcome is uncertain: {message}'
                ) from exc

            outcome: dict[str, Any] = {
                'mutation_count': None,
                'source_changed': None,
                'target_changed': None,
                'observable_change': None,
            }
            if probe is not None:
                try:
                    await asyncio.sleep(0.25)
                    outcome = await probe.evaluate(
                        """(state, points) => {
                            const signature = element => element ? {
                                tag: (element.tagName || '').toLowerCase(),
                                className: typeof element.className === 'string' ? element.className : '',
                                style: element.getAttribute?.('style') || ''
                            } : null;
                            state.observer.disconnect();
                            const sourceAfter = signature(document.elementFromPoint(...points[0]));
                            const targetAfter = signature(document.elementFromPoint(...points[1]));
                            const sourceChanged = JSON.stringify(state.sourceBefore) !== JSON.stringify(sourceAfter);
                            const targetChanged = JSON.stringify(state.targetBefore) !== JSON.stringify(targetAfter);
                            return {
                                mutation_count: state.changes,
                                source_changed: sourceChanged,
                                target_changed: targetChanged,
                                observable_change: state.changes > 0 || sourceChanged || targetChanged
                            };
                        }""",
                        [[source_x, source_y], [target_x, target_y]],
                    )
                except Exception:
                    pass
                finally:
                    await dispose_probe()
            return {
                'source_x': source_x,
                'source_y': source_y,
                'target_x': target_x,
                'target_y': target_y,
                'strategy': 'trusted-coordinate-drag',
                **outcome,
            }

    async def send_keys(self, keys: str) -> dict[str, str]:
        """Send text or one shortcut through the shared native-input boundary."""
        kind = classify_keyboard_input(keys)
        if kind == 'invalid':
            raise RuntimeError(f"Unsupported keyboard input: '{keys}'")
        async with self._lock:
            strategy = await self.input.send_keys(keys, printable=kind == 'text')
        return {'keys': keys, 'strategy': strategy}

    async def _resolve_scroll_locator(self, node: EnhancedDOMTreeNode | None):
        if node is None:
            return None
        resolved = await self.session.dom_watchdog.relocalize(node)
        return self.session.dom_watchdog.locator(resolved)

    async def _read_scroll_metrics(self, locator=None, axis: str = 'y') -> dict[str, float | str | None]:
        if locator is not None:
            return await locator.evaluate(_SCROLL_TARGET_METRICS_JS, axis)
        return await (await self.session.get_current_page()).evaluate(
            """axis => {
                const target = document.scrollingElement || document.documentElement;
                const canScroll = node => {
                    if (!node || node === document.body || node === document.documentElement) return false;
                    const style = getComputedStyle(node);
                    return axis === 'y'
                        ? !['visible', 'clip'].includes(style.overflowY)
                            && node.scrollHeight > node.clientHeight + 1
                        : !['visible', 'clip'].includes(style.overflowX)
                            && node.scrollWidth > node.clientWidth + 1;
                };
                const scrollOwner = node => {
                    while (node && node !== document.body && node !== document.documentElement) {
                        if (canScroll(node)) return node;
                        node = node.parentElement || (node.getRootNode && node.getRootNode().host);
                    }
                    return target;
                };
                const candidates = [
                    [window.innerWidth * 0.88, window.innerHeight * 0.50],
                    [window.innerWidth * 0.12, window.innerHeight * 0.50],
                    [window.innerWidth * 0.50, window.innerHeight * 0.75],
                    [window.innerWidth * 0.50, window.innerHeight * 0.25]
                ];
                const point = candidates.find(([x, y]) =>
                    scrollOwner(document.elementFromPoint(x, y)) === target
                );
                return {
                    target: 'page',
                    x: target.scrollLeft,
                    y: target.scrollTop,
                    max_x: Math.max(0, target.scrollWidth - target.clientWidth),
                    max_y: Math.max(0, target.scrollHeight - target.clientHeight),
                    client_width: target.clientWidth || window.innerWidth,
                    client_height: target.clientHeight || window.innerHeight,
                    scroll_width: target.scrollWidth,
                    scroll_height: target.scrollHeight,
                    wheel_x: point
                        ? Math.max(1, Math.min(window.innerWidth - 1, Math.round(point[0])))
                        : null,
                    wheel_y: point
                        ? Math.max(1, Math.min(window.innerHeight - 1, Math.round(point[1])))
                        : null
                };
            }""",
            axis,
        )

    async def scroll_metrics(
        self,
        node: EnhancedDOMTreeNode | None = None,
        direction: str = 'down',
    ) -> dict[str, float | str | None]:
        """Return native Firefox geometry for the requested scroll target."""
        axis = 'x' if direction in {'left', 'right'} else 'y'
        async with self._lock:
            locator = await self._resolve_scroll_locator(node)
            return await self._read_scroll_metrics(locator, axis)

    async def _wait_for_scroll_settle(self, locator, axis: str) -> dict[str, float | str | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.5
        previous = await self._read_scroll_metrics(locator, axis)
        stable_samples = 0
        keys = ('x', 'y', 'max_x', 'max_y', 'scroll_width', 'scroll_height')
        while loop.time() < deadline:
            await asyncio.sleep(0.05)
            current = await self._read_scroll_metrics(locator, axis)
            if all(abs(float(current[key]) - float(previous[key])) < 0.5 for key in keys):
                stable_samples += 1
            else:
                stable_samples = 0
            previous = current
            if stable_samples >= 5:
                break
        return previous

    @staticmethod
    def _scroll_progress(
        before: dict[str, float | str | None],
        after: dict[str, float | str | None],
        axis: str,
        direction: str,
    ) -> int:
        movement = float(after[axis]) - float(before[axis])
        progress = movement if direction in {'down', 'right'} else -movement
        return max(0, int(round(progress)))

    async def scroll(
        self,
        node: EnhancedDOMTreeNode | None,
        *,
        direction: str,
        amount: int,
    ) -> dict[str, Any]:
        """Send a wheel gesture, measure it, and use a recorded DOM fallback."""
        axis = 'x' if direction in {'left', 'right'} else 'y'
        x = amount if direction == 'right' else -amount if direction == 'left' else 0
        y = amount if direction == 'down' else -amount if direction == 'up' else 0
        async with self._lock:
            locator = await self._resolve_scroll_locator(node)
            before = await self._read_scroll_metrics(locator, axis)
            before_position = float(before[axis])
            before_maximum = float(before[f'max_{axis}'])
            already_at_boundary = (
                before_position >= before_maximum - 1
                if direction in {'down', 'right'}
                else before_position <= 1
            )
            delivery = 'none'
            after = before
            if amount > 0 and not already_at_boundary:
                wheel_x = before.get('wheel_x')
                wheel_y = before.get('wheel_y')
                # Playwright's Firefox wheel input is trusted for the page, but
                # can scroll the top-level document even when its event target
                # is inside an overflow container. Use exact DOM scrolling for
                # an explicit element target rather than leaking into the page.
                if before['target'] == 'page' and wheel_x is not None and wheel_y is not None:
                    await self.input.wheel(int(wheel_x), int(wheel_y), x, y)
                    delivery = 'wheel'
                    after = await self._wait_for_scroll_settle(locator, axis)
                if self._scroll_progress(before, after, axis, direction) == 0:
                    await self.input.dom_scroll(locator, x, y)
                    delivery = 'dom-target' if before['target'] != 'page' else 'dom-fallback'
                    after = await self._wait_for_scroll_settle(locator, axis)

        moved = self._scroll_progress(before, after, axis, direction)
        position = max(0, int(round(float(after[axis]))))
        maximum = max(0, int(round(float(after[f'max_{axis}']))))
        at_boundary = position >= maximum - 1 if direction in {'down', 'right'} else position <= 1
        self.session.dom_watchdog.current_dom_state = None
        self.session.dom_watchdog.enhanced_dom_tree = None
        return {
            'scrolled': moved > 0,
            'requested': amount,
            'moved': moved,
            'position': position,
            'maximum': maximum,
            'at_boundary': at_boundary,
            'no_progress': moved == 0,
            'target': str(after['target']),
            'delivery': delivery,
            'client_width': int(round(float(after['client_width']))),
            'client_height': int(round(float(after['client_height']))),
        }

    async def scroll_to_text(self, text: str) -> dict[str, bool | str]:
        """Find and reveal text while serializing the viewport mutation."""
        async with self._lock:
            found = await (await self.session.get_current_page()).evaluate(
                """text => {
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    while (walker.nextNode()) {
                        if ((walker.currentNode.textContent || '').includes(text)) {
                            walker.currentNode.parentElement?.scrollIntoView({block: 'center'});
                            return true;
                        }
                    }
                    return false;
                }""",
                text,
            )
        if not found:
            raise RuntimeError(f"Text '{text}' not found on page")
        self.session.dom_watchdog.current_dom_state = None
        self.session.dom_watchdog.enhanced_dom_tree = None
        return {'scrolled': True, 'delivery': 'dom-target-reveal'}

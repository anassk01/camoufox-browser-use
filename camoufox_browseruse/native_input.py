"""Low-level Firefox input delivery used by interaction transactions."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Locator

    from camoufox_browseruse.session import CamoufoxSession


class NativeInputNotDispatched(RuntimeError):
    """The input transport failed before an effect-producing event was sent."""


class FirefoxNativeInput:
    """Deliver trusted Playwright input without deciding action correctness."""

    def __init__(self, session: 'CamoufoxSession') -> None:
        self.session = session
        self._random = random.Random()
        self._pointer_page: object | None = None
        self._pointer_position: tuple[float, float] = (0.0, 0.0)

    @property
    def humanized(self) -> bool:
        return bool(self.session._humanize_interactions_native)

    def _typing_delays(self, text: str) -> list[float]:
        if not self.humanized or len(text) < 2:
            return [0.0] * len(text)
        delays = []
        for character in text:
            delay = self._random.uniform(0.035, 0.105)
            if character in '.,;:!?':
                delay += self._random.uniform(0.045, 0.130)
            elif character.isspace():
                delay += self._random.uniform(0.010, 0.045)
            delays.append(delay)

        # Keep long inputs within Browser-Use's action deadline while retaining
        # non-uniform intervals for the text that produces keyboard events.
        total = sum(delays[:-1])
        if total > 4.0:
            scale = 4.0 / total
            delays = [delay * scale for delay in delays]
        delays[-1] = 0.0
        return delays

    async def _type_printable(self, text: str) -> None:
        keyboard = (await self.session.get_current_page()).keyboard
        if not self.humanized:
            await keyboard.type(text)
            return
        for character, delay in zip(text, self._typing_delays(text), strict=True):
            if character == '\n':
                await keyboard.press('Enter')
            elif character == '\t':
                await keyboard.press('Tab')
            elif character == '\r':
                continue
            else:
                await keyboard.type(character)
            if delay:
                await asyncio.sleep(delay)

    async def _position_pointer(self, x: int, y: int) -> None:
        page = await self.session.get_current_page()
        mouse = page.mouse
        if self._pointer_page is not page:
            # Pointer coordinates belong to one top-level page. A tab switch
            # starts a fresh trajectory instead of inheriting meaningless
            # coordinates from the previous viewport.
            self._pointer_page = page
            self._pointer_position = (0.0, 0.0)
        if not self.humanized:
            await mouse.move(x, y)
            self._pointer_position = (x, y)
            return

        start_x, start_y = self._pointer_position
        dx, dy = x - start_x, y - start_y
        distance = (dx * dx + dy * dy) ** 0.5
        if distance <= 1:
            return

        normal_x = -dy / distance
        normal_y = dx / distance
        curve_size = self._random.uniform(0.06, 0.22) * min(distance, 300)
        direction = self._random.choice((-1, 1))

        def control_points(sign: int) -> tuple[tuple[float, float], tuple[float, float]]:
            curve = sign * curve_size
            return (
                (
                    start_x + dx * 0.33 + normal_x * curve,
                    start_y + dy * 0.33 + normal_y * curve,
                ),
                (
                    start_x + dx * 0.67 + normal_x * curve * 0.55,
                    start_y + dy * 0.67 + normal_y * curve * 0.55,
                ),
            )

        control_1, control_2 = control_points(direction)
        viewport = page.viewport_size
        if viewport:
            def clearance(points: tuple[tuple[float, float], tuple[float, float]]) -> float:
                return min(
                    min(px, py, viewport['width'] - px, viewport['height'] - py)
                    for px, py in points
                )

            current_clearance = clearance((control_1, control_2))
            if current_clearance < 1:
                alternative = control_points(-direction)
                if clearance(alternative) > current_clearance:
                    control_1, control_2 = alternative
        # A few curved segments keep protocol work bounded. Playwright emits
        # intermediate trusted mousemove events for each segment itself.
        for t in (0.16, 0.34, 0.55, 0.76, 1.0):
            inverse = 1 - t
            point_x = (
                inverse ** 3 * start_x
                + 3 * inverse * inverse * t * control_1[0]
                + 3 * inverse * t * t * control_2[0]
                + t ** 3 * x
            )
            point_y = (
                inverse ** 3 * start_y
                + 3 * inverse * inverse * t * control_1[1]
                + 3 * inverse * t * t * control_2[1]
                + t ** 3 * y
            )
            await mouse.move(round(point_x), round(point_y), steps=4)
            if t < 1:
                await asyncio.sleep(self._random.uniform(0.006, 0.018))
        self._pointer_position = (x, y)

    async def click(self, locator: 'Locator', *, button: str) -> str:
        try:
            box = await locator.bounding_box(timeout=1500)
            if box is None:
                raise RuntimeError('Target has no clickable bounding box.')
            x = round(box['x'] + box['width'] / 2)
            y = round(box['y'] + box['height'] / 2)
            mouse = (await self.session.get_current_page()).mouse
            await self._position_pointer(x, y)
            await asyncio.sleep(0.05)

            # Re-read a dynamic locator after moving. Refuse to press if the
            # target detached, shifted under an overlay, or stopped owning its
            # center point. No activating event has been sent at this stage.
            current = await locator.bounding_box(timeout=1000)
            if current is None:
                raise RuntimeError('Target detached while positioning the pointer.')
            current_x = round(current['x'] + current['width'] / 2)
            current_y = round(current['y'] + current['height'] / 2)
            if abs(current_x - x) > 1 or abs(current_y - y) > 1:
                await self._position_pointer(current_x, current_y)
                await asyncio.sleep(0.05)
            owns_point = await locator.evaluate(
                """el => {
                    const rect = el.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    let hit = document.elementFromPoint(
                        x,
                        y
                    );
                    while (hit && hit.shadowRoot) {
                        const deeper = typeof hit.shadowRoot.elementFromPoint === 'function'
                            ? hit.shadowRoot.elementFromPoint(x, y)
                            : hit.shadowRoot.elementsFromPoint?.(x, y)?.[0];
                        if (!deeper || deeper === hit) break;
                        hit = deeper;
                    }
                    return Boolean(hit && (hit === el || el.contains(hit)));
                }""",
                timeout=1000,
            )
            if not owns_point:
                raise RuntimeError('Target became covered before activation.')
        except Exception as exc:
            if isinstance(exc, NativeInputNotDispatched):
                raise
            raise NativeInputNotDispatched(str(exc).splitlines()[0]) from exc

        await mouse.down(button=button)
        await asyncio.sleep(0.05)
        await mouse.up(button=button)
        return 'trusted-pointer-click'

    async def click_at(self, x: int, y: int, *, button: str) -> None:
        mouse = (await self.session.get_current_page()).mouse
        try:
            await self._position_pointer(x, y)
        except Exception as exc:
            raise NativeInputNotDispatched(str(exc).splitlines()[0]) from exc
        await asyncio.sleep(0.05)
        await mouse.down(button=button)
        await asyncio.sleep(0.05)
        await mouse.up(button=button)

    async def drag_at(
        self,
        source_x: int,
        source_y: int,
        target_x: int,
        target_y: int,
    ) -> None:
        """Deliver one trusted press, move, and release pointer sequence."""
        page = await self.session.get_current_page()
        mouse = page.mouse
        try:
            await self._position_pointer(source_x, source_y)
        except Exception as exc:
            raise NativeInputNotDispatched(str(exc).splitlines()[0]) from exc

        await asyncio.sleep(0.05)
        await mouse.down(button='left')
        try:
            await asyncio.sleep(0.08)
            if self.humanized:
                await self._position_pointer(target_x, target_y)
            else:
                await mouse.move(target_x, target_y, steps=12)
                self._pointer_position = (target_x, target_y)
            await asyncio.sleep(0.08)
            await mouse.up(button='left')
        except BaseException:
            # Avoid leaving Firefox with a pressed pointer if transport or task
            # cancellation interrupts the gesture. The page outcome is still
            # uncertain once mouse.down has returned.
            try:
                await asyncio.shield(mouse.up(button='left'))
            except Exception:
                pass
            raise

    async def type_text(
        self,
        locator: 'Locator',
        text: str,
        *,
        clear: bool,
        direct_fill: bool,
    ) -> str:
        if direct_fill:
            await locator.fill(text, timeout=2500)
            return 'fill'

        await locator.focus(timeout=2000)
        if clear:
            keyboard = (await self.session.get_current_page()).keyboard
            # The browser fingerprint can report a different OS from the host;
            # Control+A then moves the caret instead of selecting on macOS.
            await locator.select_text(timeout=2000)
            await keyboard.press('Backspace')
            remaining = await locator.evaluate(
                "el => el.isContentEditable ? (el.innerText || '') : String(el.value || '')"
            )
            if remaining:
                raise RuntimeError('Could not clear the target; replacement text was not sent.')
        else:
            await locator.evaluate(
                """el => {
                    if (typeof el.setSelectionRange === 'function') {
                        const end = String(el.value || '').length;
                        el.setSelectionRange(end, end);
                    } else if (el.isContentEditable) {
                        const range = document.createRange();
                        range.selectNodeContents(el);
                        range.collapse(false);
                        const selection = window.getSelection();
                        selection.removeAllRanges();
                        selection.addRange(range);
                    }
                }"""
            )
        await self._type_printable(text)
        return 'keyboard'

    async def send_keys(self, keys: str, *, printable: bool) -> str:
        if printable:
            await self._type_printable(keys)
            return 'humanized-keyboard' if self.humanized else 'keyboard'
        await (await self.session.get_current_page()).keyboard.press(keys)
        return 'key-press'

    async def wheel(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        mouse = (await self.session.get_current_page()).mouse
        await self._position_pointer(x, y)
        await mouse.wheel(delta_x, delta_y)

    async def dom_scroll(self, locator: 'Locator | None', x: int, y: int) -> None:
        if locator is None:
            await (await self.session.get_current_page()).evaluate(
                """delta => window.scrollBy({
                    left: delta.x, top: delta.y, behavior: 'instant'
                })""",
                {'x': x, 'y': y},
            )
            return
        await locator.evaluate(
            """(element, delta) => {
                const parentOf = node => node.parentElement
                    || (node.getRootNode && node.getRootNode().host)
                    || null;
                const canScroll = node => {
                    const style = getComputedStyle(node);
                    return delta.axis === 'y'
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
                target.scrollBy({left: delta.x, top: delta.y, behavior: 'instant'});
            }""",
            {'x': x, 'y': y, 'axis': 'y' if y else 'x'},
        )

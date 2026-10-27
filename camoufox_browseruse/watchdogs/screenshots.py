"""Screenshots and visual element highlights."""

from __future__ import annotations

import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from browser_use.browser.events import ScreenshotEvent
from browser_use.browser.python_highlights import get_cross_platform_font
from browser_use.dom.views import EnhancedDOMTreeNode
from PIL import Image, ImageDraw

from .base import NativeWatchdog

logger = logging.getLogger(__name__)


def label_screenshot(
    screenshot: bytes, selector_map: dict[int, EnhancedDOMTreeNode], viewport_width: int, filter_ids: bool = True,
) -> bytes:
    """Write each text-less element's index at its top centre, where the upstream agent prompt says it is.

    Unlike upstream's python_highlights, nothing is drawn outside the element and
    there are no outlines. Labels shrink to fit small elements and are
    translucent, so selection fills, checkmarks and the page's own borders stay visible.
    """
    with Image.open(BytesIO(screenshot)) as source:
        image = source.convert('RGBA')
    ratio = image.width / max(1, viewport_width)
    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for index, node in selector_map.items():
        rect = node.absolute_position
        # Upstream's rule: elements with readable text are identified by that text.
        if not rect or (filter_ids and len(node.get_meaningful_text_for_llm()) >= 3):
            continue
        x1, y1 = max(0.0, rect.x * ratio), max(0.0, rect.y * ratio)
        x2, y2 = min(image.width, (rect.x + rect.width) * ratio), min(image.height, (rect.y + rect.height) * ratio)
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        size = max(8, min(13 * ratio, (y2 - y1) * 0.4))
        font = get_cross_platform_font(round(size))
        left, top, right, bottom = draw.textbbox((0, 0), str(index), font=font)
        width, height = right - left + 4, bottom - top + 2
        x = max(x1, (x1 + x2 - width) / 2)
        draw.rectangle((x, y1, x + width, y1 + height), fill=(0, 0, 0, 140))
        draw.text((x + 2 - left, y1 + 1 - top), str(index), fill=(255, 255, 255, 255), font=font)
    output = BytesIO()
    Image.alpha_composite(image, overlay).convert('RGB').save(output, format='PNG')
    return output.getvalue()


class ScreenshotWatchdog(NativeWatchdog):
    HANDLERS = {ScreenshotEvent: 'on_ScreenshotEvent'}

    async def on_ScreenshotEvent(self, event: ScreenshotEvent) -> str:
        await self.remove_highlights()
        data = await self.capture(full_page=event.full_page, clip=event.clip)
        return base64.b64encode(data).decode('ascii')

    async def capture(
        self,
        path: str | Path | None = None,
        full_page: bool = False,
        format: str = 'png',
        quality: int | None = None,
        clip: dict[str, Any] | None = None,
    ) -> bytes:
        page = await self.browser_session.get_current_page()
        kind = 'jpeg' if format.lower() in {'jpeg', 'jpg'} else 'png'
        options: dict[str, Any] = {'full_page': full_page, 'type': kind}
        if clip:
            options['clip'] = clip
        if quality is not None and kind == 'jpeg':
            options['quality'] = quality
        data = await page.screenshot(**options)
        if path:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        return data

    async def capture_element(
        self,
        selector: str,
        path: str | Path | None = None,
        format: str = 'png',
        quality: int | None = None,
    ) -> bytes:
        kind = 'jpeg' if format.lower() in {'jpeg', 'jpg'} else 'png'
        options: dict[str, Any] = {'type': kind}
        if quality is not None and kind == 'jpeg':
            options['quality'] = quality
        data = await (await self.browser_session.get_current_page()).locator(selector).first.screenshot(**options)
        if path:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        return data

    async def add_highlights(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
        elements = [
            {
                'index': index,
                'x': node.absolute_position.x,
                'y': node.absolute_position.y,
                'width': node.absolute_position.width,
                'height': node.absolute_position.height,
            }
            for index, node in selector_map.items()
            if node.absolute_position and node.absolute_position.width > 0 and node.absolute_position.height > 0
        ]
        if not elements:
            return
        await (await self.browser_session.get_current_page()).evaluate(
            """elements => {
                document.getElementById('browser-use-debug-highlights')?.remove();
                const container = document.createElement('div');
                container.id = 'browser-use-debug-highlights';
                container.dataset.browserUseHighlight = 'container';
                container.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:2147483647';
                for (const item of elements) {
                    const box = document.createElement('div');
                    box.dataset.browserUseHighlight = 'element';
                    box.style.cssText = `position:absolute;left:${item.x}px;top:${item.y}px;width:${item.width}px;height:${item.height}px;outline:2px dashed #4a90e2`;
                    const label = document.createElement('div');
                    label.textContent = String(item.index);
                    label.style.cssText = 'position:absolute;top:-18px;background:#4a90e2;color:white;padding:1px 5px;font:700 11px monospace';
                    box.appendChild(label); container.appendChild(box);
                }
                document.body.appendChild(container);
            }""",
            elements,
        )

    async def remove_highlights(self) -> None:
        try:
            await (await self.browser_session.get_current_page()).evaluate(
                """() => {
                    document.getElementById('browser-use-debug-highlights')?.remove();
                    document.querySelectorAll('[data-browser-use-highlight]').forEach(el => el.remove());
                }"""
            )
        except Exception as exc:
            logger.debug('Removing highlights failed: %s', exc)

    async def highlight_coordinate(self, x: int, y: int) -> None:
        try:
            await (await self.browser_session.get_current_page()).evaluate(
                """([x, y]) => {
                    const marker = document.createElement('div');
                    marker.style.cssText = `position:fixed;left:${x - 10}px;top:${y - 10}px;width:20px;height:20px;border:2px solid #f44;border-radius:50%;pointer-events:none;z-index:2147483647`;
                    document.body.appendChild(marker); setTimeout(() => marker.remove(), 500);
                }""",
                [x, y],
            )
        except Exception:
            pass

    async def highlight_node(self, node: EnhancedDOMTreeNode) -> None:
        try:
            await self.browser_session.dom_watchdog.locator(node).evaluate(
                "el => el.style.outline = '2px solid rgba(255, 115, 0, 0.8)'",
                timeout=1000,
            )
        except Exception:
            pass

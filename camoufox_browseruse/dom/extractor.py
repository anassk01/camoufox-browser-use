"""Bounded Playwright frame-tree collector for Browser-Use DOM state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Frame, Page

from camoufox_browseruse.dom.accessibility import merge_accessibility
from camoufox_browseruse.dom.extractor_js import DOM_EXTRACTOR_JS
from camoufox_browseruse.helpers import (
    MAX_SAFE_ATTRIBUTE_VALUE_LENGTH,
    MAX_SEMANTIC_EVIDENCE_LENGTH,
    SENSITIVE_ATTRIBUTE_MARKERS,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameExtractionOptions:
    """BrowserProfile settings that bound DOM traversal."""

    cross_origin_iframes: bool = True
    paint_order_filtering: bool = True
    max_iframes: int = 100
    max_iframe_depth: int = 5


class FrameRegistry:
    """Keep locator frame IDs stable while a Playwright Frame remains attached."""

    def __init__(self) -> None:
        self._main_frame: Frame | None = None
        self._next_id = 1
        self._ids_by_frame: dict[Frame, str] = {}
        self.frames_by_id: dict[str, Frame] = {}

    def begin(self, page: Page) -> None:
        if self._main_frame is not page.main_frame:
            self.clear()
            self._main_frame = page.main_frame
        self._ids_by_frame[page.main_frame] = 'main'
        self.frames_by_id['main'] = page.main_frame

    def id_for(self, frame: Frame) -> str:
        existing = self._ids_by_frame.get(frame)
        if existing:
            return existing
        frame_id = f'frame-{self._next_id}'
        self._next_id += 1
        self._ids_by_frame[frame] = frame_id
        self.frames_by_id[frame_id] = frame
        return frame_id

    def finish(self, active_frames: set[Frame]) -> None:
        for frame, frame_id in list(self._ids_by_frame.items()):
            if frame not in active_frames:
                self._ids_by_frame.pop(frame, None)
                self.frames_by_id.pop(frame_id, None)

    def clear(self) -> None:
        self._main_frame = None
        self._next_id = 1
        self._ids_by_frame.clear()
        self.frames_by_id.clear()


def _origin(
    url: str,
    inherited: tuple[str, str, int | None] | None = None,
) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme in {'about', 'javascript'} or not parsed.scheme:
        return inherited
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return (parsed.scheme, parsed.hostname or '', port)
    default_port = 443 if parsed.scheme == 'https' else 80
    return (parsed.scheme, parsed.hostname, port or default_port)


async def _effective_frame_url(frame: Frame, parent_url: str) -> str:
    """Resolve iframe src when its live URL is temporarily blank during navigation."""
    if frame.url and frame.url != 'about:blank':
        return frame.url
    if frame.parent_frame is None:
        return frame.url
    try:
        src = await (await frame.frame_element()).get_attribute('src')
    except Exception:
        src = None
    if src and src not in {'about:blank', 'javascript:void(0)'}:
        return urljoin(parent_url, src)
    return frame.url or 'about:blank'


async def _selected_frames(page: Page, options: FrameExtractionOptions) -> list[tuple[Frame, int]]:
    """Traverse the live frame tree breadth-first with explicit safety bounds."""
    selected: list[tuple[Frame, int]] = [(page.main_frame, 0)]
    queue: list[tuple[Frame, int, str, tuple[str, str, int | None] | None]] = [
        (page.main_frame, 0, page.main_frame.url, _origin(page.main_frame.url))
    ]
    iframe_count = 0
    while queue and iframe_count < max(0, options.max_iframes):
        parent, depth, parent_url, parent_origin = queue.pop(0)
        if depth >= max(0, options.max_iframe_depth):
            continue
        for child in parent.child_frames:
            if iframe_count >= max(0, options.max_iframes):
                break
            child_url = await _effective_frame_url(child, parent_url)
            child_origin = _origin(child_url, parent_origin)
            if not options.cross_origin_iframes and child_origin != parent_origin:
                continue
            iframe_count += 1
            selected.append((child, depth + 1))
            queue.append((child, depth + 1, child_url, child_origin))
    return selected


async def _frame_geometry(frame: Frame, *, paint_order_filtering: bool) -> tuple[float, float] | None:
    """Return child content origin in main-viewport coordinates, or None when hidden."""
    if frame.parent_frame is None:
        return (0.0, 0.0)
    frame_element = await frame.frame_element()
    box = await frame_element.bounding_box()
    if not box or box.get('width', 0) <= 0 or box.get('height', 0) <= 0:
        return None
    details = await frame_element.evaluate(
        """(el, filterPaint) => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            let exposed = style.display !== 'none' && style.visibility !== 'hidden'
                && style.visibility !== 'collapse' && style.opacity !== '0';
            if (exposed && filterPaint && rect.right > 0 && rect.bottom > 0
                    && rect.left < innerWidth && rect.top < innerHeight) {
                const left = Math.max(0, rect.left);
                const right = Math.min(innerWidth - 1, rect.right - 1);
                const topEdge = Math.max(0, rect.top);
                const bottom = Math.min(innerHeight - 1, rect.bottom - 1);
                const points = [
                    [(left + right) / 2, (topEdge + bottom) / 2],
                    [left + 1, topEdge + 1], [right - 1, topEdge + 1],
                    [left + 1, bottom - 1], [right - 1, bottom - 1],
                ];
                exposed = points.some(([x, y]) => {
                    const hit = document.elementFromPoint(x, y);
                    return Boolean(hit && (hit === el || el.contains(hit)));
                });
            }
            return {exposed, clientLeft: el.clientLeft || 0, clientTop: el.clientTop || 0};
        }""",
        paint_order_filtering,
    )
    if not details.get('exposed', True):
        return None
    # Playwright reports iframe element boxes in main-frame viewport coordinates.
    return (
        float(box.get('x', 0.0)) + float(details.get('clientLeft', 0.0)),
        float(box.get('y', 0.0)) + float(details.get('clientTop', 0.0)),
    )


async def _extract_frame(
    frame: Frame,
    frame_id: str,
    depth: int,
    options: FrameExtractionOptions,
) -> list[dict[str, Any]]:
    if frame.is_detached():
        return []
    offset = await _frame_geometry(frame, paint_order_filtering=options.paint_order_filtering)
    if offset is None:
        return []
    args = {
        'frameId': frame_id,
        'frameUrl': '' if depth == 0 else frame.url,
        'frameDepth': depth,
        'paintOrderFiltering': options.paint_order_filtering,
        'maxAttributeValueLength': MAX_SAFE_ATTRIBUTE_VALUE_LENGTH,
        'maxExtractionMs': 5000,
        'maxSemanticEvidenceLength': MAX_SEMANTIC_EVIDENCE_LENGTH,
        'sensitiveAttributeMarkers': list(SENSITIVE_ATTRIBUTE_MARKERS),
        'maxElements': 1000,
    }
    extracted_result, aria_result = await asyncio.gather(
        frame.evaluate(DOM_EXTRACTOR_JS, args),
        frame.locator('body').aria_snapshot(timeout=3000, mode='default', boxes=True),
        return_exceptions=True,
    )
    if isinstance(extracted_result, BaseException):
        raise extracted_result
    extracted = extracted_result
    if not isinstance(extracted, list):
        return []
    if isinstance(aria_result, str):
        merge_accessibility(extracted, aria_result)
    else:
        # Preserve a complete DOM observation even when native accessibility
        # collection is unavailable for a transient/detached frame.
        merge_accessibility(extracted, '')
    offset_x, offset_y = offset
    for element in extracted:
        if not isinstance(element, dict):
            continue
        rect = element.get('rect')
        if isinstance(rect, dict):
            rect['x'] = float(rect.get('x', 0.0)) + offset_x
            rect['y'] = float(rect.get('y', 0.0)) + offset_y
    return [element for element in extracted if isinstance(element, dict)]


async def extract_interactive_elements(
    page: Page,
    registry: FrameRegistry | None = None,
    options: FrameExtractionOptions | None = None,
) -> list[dict[str, Any]]:
    """Extract a deterministic, bounded snapshot from every selected live frame."""
    registry = registry or FrameRegistry()
    options = options or FrameExtractionOptions()
    registry.begin(page)
    selected = await _selected_frames(page, options)
    active_frames = {frame for frame, _ in selected}
    registry.finish(active_frames)

    tasks = [
        _extract_frame(frame, registry.id_for(frame), depth, options)
        for frame, depth in selected
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    extracted_by_frame: dict[Frame, list[dict[str, Any]]] = {}
    for (frame, depth), result in zip(selected, results, strict=True):
        if isinstance(result, BaseException):
            logger.debug('Failed to extract DOM elements from frame depth=%s url=%s: %s', depth, frame.url, result)
            continue
        extracted_by_frame[frame] = result

    # A frame evaluates its own document, so its root nodes cannot discover the
    # embedding <iframe> in JavaScript. Join that edge here using the frame
    # element's stable document ordinal. This gives the observation one tree
    # spanning the main document and nested same/cross-origin frames.
    for frame, _depth in selected[1:]:
        child_elements = extracted_by_frame.get(frame)
        parent = frame.parent_frame
        parent_elements = extracted_by_frame.get(parent) if parent is not None else None
        if not child_elements or not parent_elements:
            continue
        try:
            frame_element = await frame.frame_element()
            frame_ordinal = await frame_element.evaluate(
                """el => Array.from(el.ownerDocument.querySelectorAll('iframe, frame')).indexOf(el)"""
            )
        except Exception as exc:
            logger.debug('Failed to link iframe hierarchy for url=%s: %s', frame.url, exc)
            continue
        container = next(
            (
                element for element in parent_elements
                if str(element.get('tagName', '')).lower() in {'iframe', 'frame'}
                and element.get('frame_ordinal') == frame_ordinal
            ),
            None,
        )
        if not container:
            continue
        container['child_frame_id'] = registry.id_for(frame)
        container_tree_id = container.get('tree_id')
        for element in child_elements:
            if element.get('parent_tree_id') is None:
                element['parent_tree_id'] = container_tree_id

    elements: list[dict[str, Any]] = []
    for frame, _depth in selected:
        elements.extend(extracted_by_frame.get(frame, []))
    return elements

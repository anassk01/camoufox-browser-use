"""Only the Browser-Use tools whose upstream implementation requires CDP."""

import asyncio
import io
import math
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw
from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import ScrollEvent, GetDropdownOptionsEvent, SelectDropdownOptionEvent
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.service import (
    Tools,
    _build_find_elements_js,
    _build_search_page_js,
    _format_find_results,
    _format_search_results,
)
from browser_use.tools.views import (
    FindElementsAction,
    SaveAsPdfAction,
    ScreenshotAction,
    SearchPageAction,
    ScrollAction,
    UploadFileAction,
    GetDropdownOptionsAction,
    SelectDropdownOptionAction,
)
from pydantic import BaseModel, Field


MAX_EVALUATE_OUTPUT_CHARS = 20000
# Longer than a normal awaited fetch; a synchronous loop that runs this long has frozen the page.
EVALUATE_TIMEOUT_SECONDS = 20.0


def _bounded_evaluate_output(result: object, max_chars: int = MAX_EVALUATE_OUTPUT_CHARS) -> tuple[str, bool]:
    """Keep arbitrary page-script output useful without making it durable or unbounded."""
    text = str(result)
    if len(text) <= max_chars:
        return text, False
    tail_chars = min(2000, max_chars // 5)
    marker = ''
    for _ in range(3):
        head_chars = max(0, max_chars - tail_chars - len(marker))
        omitted = len(text) - head_chars - tail_chars
        marker = f'\n... [{omitted} characters omitted; rerun a narrower query for the missing section] ...\n'
    head_chars = max(0, max_chars - tail_chars - len(marker))
    return text[:head_chars] + marker + text[-tail_chars:], True


class DragAndDropAction(BaseModel):
    source_x: int = Field(ge=0, description='Source X coordinate in the screenshot')
    source_y: int = Field(ge=0, description='Source Y coordinate in the screenshot')
    target_x: int = Field(ge=0, description='Target X coordinate in the screenshot')
    target_y: int = Field(ge=0, description='Target Y coordinate in the screenshot')


def _session(browser_session: BrowserSession):
    from camoufox_browseruse.session import CamoufoxSession
    if not isinstance(browser_session, CamoufoxSession):
        raise TypeError(f'Camoufox tool used with {type(browser_session).__name__}')
    return browser_session


def _safe_output_name(raw_name: str, suffix: str) -> str | None:
    raw_name = raw_name.strip()
    if not raw_name or '..' in raw_name or '/' in raw_name or '\\' in raw_name:
        return None
    name = Path(raw_name).name
    return name if name.lower().endswith(suffix) else name + suffix


def _image_to_viewport_coordinates(
    x: int,
    y: int,
    browser_session: BrowserSession,
) -> tuple[int, int]:
    if browser_session.llm_screenshot_size and browser_session._original_viewport_size:
        image_width, image_height = browser_session.llm_screenshot_size
        viewport_width, viewport_height = browser_session._original_viewport_size
        return (
            int(x / image_width * viewport_width),
            int(y / image_height * viewport_height),
        )
    return x, y


def _viewport_rect_to_image_coordinates(
    rect: dict[str, float],
    browser_session: BrowserSession,
) -> dict[str, int]:
    scale_x = scale_y = 1.0
    if browser_session.llm_screenshot_size and browser_session._original_viewport_size:
        image_width, image_height = browser_session.llm_screenshot_size
        viewport_width, viewport_height = browser_session._original_viewport_size
        scale_x = image_width / viewport_width
        scale_y = image_height / viewport_height
    x = round(rect['x'] * scale_x)
    y = round(rect['y'] * scale_y)
    width = round(rect['width'] * scale_x)
    height = round(rect['height'] * scale_y)
    return {
        'x': x,
        'y': y,
        'width': width,
        'height': height,
        'center_x': round((rect['x'] + rect['width'] / 2) * scale_x),
        'center_y': round((rect['y'] + rect['height'] / 2) * scale_y),
    }


def register_camoufox_tools(tools: Tools) -> None:
    """Keep upstream event-driven tools and replace only CDP-dependent operations."""

    tools.set_coordinate_clicking(True)

    @tools.registry.action(
        'List exact options in a native select or ARIA dropdown. For a custom control, opens its owned menu; options below the visible scroll area are included. Inspect before selecting.',
        param_model=GetDropdownOptionsAction,
        terminates_sequence=True,
    )
    async def dropdown_options(params: GetDropdownOptionsAction, browser_session: BrowserSession):
        session = _session(browser_session)
        node = await session.get_element_by_index(params.index)
        if node is None:
            return ActionResult(error='Dropdown index is stale. Inspect the current state.')
        try:
            event = session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
            result = await event.event_result(timeout=10, raise_if_any=True, raise_if_none=True)
            return ActionResult(extracted_content=result['short_term_memory'], long_term_memory=result['long_term_memory'])
        except Exception as exc:
            return ActionResult(error=f'Could not inspect dropdown: {exc}')

    @tools.registry.action(
        'Select one exact option by text in a native select or ARIA dropdown. Use the dropdown control index, not its option index. Opens and scrolls its owned menu, commits one selection, and verifies it. Use dropdown_options first if the exact text is unknown.',
        param_model=SelectDropdownOptionAction,
        terminates_sequence=True,
    )
    async def select_dropdown(params: SelectDropdownOptionAction, browser_session: BrowserSession):
        session = _session(browser_session)
        node = await session.get_element_by_index(params.index)
        if node is None:
            return ActionResult(error='Dropdown index is stale. Inspect the current state.')
        try:
            event = session.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=params.text))
            result = await event.event_result(timeout=12, raise_if_any=True, raise_if_none=True)
            if result.get('success') != 'true':
                return ActionResult(error=result.get('error', 'Dropdown selection is unconfirmed.'))
            return ActionResult(extracted_content=result['message'], long_term_memory=result['message'])
        except Exception as exc:
            return ActionResult(error=f'Could not select dropdown option: {exc}')

    @tools.registry.action(
        'Drag from source screenshot coordinates to target screenshot coordinates using a trusted pointer gesture. Use for chess pieces, cards, sliders, canvas items, and other press-hold-move-release controls. Verify the result in the next browser state.',
        param_model=DragAndDropAction,
        terminates_sequence=True,
    )
    async def drag_and_drop(params: DragAndDropAction, browser_session: BrowserSession):
        session = _session(browser_session)
        source_x, source_y = _image_to_viewport_coordinates(
            params.source_x,
            params.source_y,
            session,
        )
        target_x, target_y = _image_to_viewport_coordinates(
            params.target_x,
            params.target_y,
            session,
        )
        try:
            metadata = await session.action_watchdog.drag_coordinates(
                source_x,
                source_y,
                target_x,
                target_y,
            )
            memory = (
                f'Pointer drag gesture dispatched from ({params.source_x}, {params.source_y}) '
                f'to ({params.target_x}, {params.target_y}).'
            )
            if metadata.get('source_changed') or metadata.get('target_changed'):
                outcome = ' The content at a drag endpoint changed; verify the intended result.'
            elif metadata.get('observable_change'):
                outcome = ' DOM activity occurred, but both endpoints look unchanged; acceptance is unconfirmed.'
            elif metadata.get('observable_change') is False:
                outcome = ' No page response was observed; the drag may have missed or been rejected.'
            else:
                outcome = ' Page response could not be measured; verify the intended result.'
            return ActionResult(
                extracted_content=memory + outcome,
                long_term_memory=memory + outcome,
                metadata=metadata,
            )
        except Exception as exc:
            return ActionResult(error=f'Failed to drag: {type(exc).__name__}: {exc}')

    @tools.registry.action(
        """Scroll by pages. REQUIRED: down=True/False (True=scroll down, False=scroll up, default=True). Optional: pages=0.5-10.0 (default 1.0). Use index for scroll elements (dropdowns/custom UI). High pages (10) reaches bottom. Multi-page scrolls sequentially. Viewport-based height.""",
        param_model=ScrollAction,
    )
    async def scroll(params: ScrollAction, browser_session: BrowserSession):
        session = _session(browser_session)
        pages = float(params.pages)
        if not math.isfinite(pages) or pages <= 0 or pages > 10:
            return ActionResult(error='Scroll pages must be greater than 0 and at most 10.')

        node = None
        if params.index is not None and params.index != 0:
            node = await session.get_element_by_index(params.index)
            if node is None:
                return ActionResult(error=f'Element index {params.index} not found in browser state')

        direction = 'down' if params.down else 'up'
        try:
            initial = await session.action_watchdog.scroll_metrics(node, direction)
            page_size = max(1, int(round(float(initial['client_height']))))
            position = float(initial['y'])
            maximum = float(initial['max_y'])

            if pages < 1:
                amounts = [max(1, int(round(pages * page_size)))]
            else:
                whole_pages = int(pages)
                amounts = [page_size] * whole_pages
                fraction = pages - whole_pages
                if fraction > 0:
                    amounts.append(max(1, int(round(fraction * page_size))))

            outcomes: list[dict] = []

            async def dispatch_scroll(amount: int) -> dict:
                event = session.event_bus.dispatch(
                    ScrollEvent(direction=direction, amount=amount, node=node)
                )
                await event
                outcome = await event.event_result(raise_if_any=True, raise_if_none=True)
                if not isinstance(outcome, dict):
                    raise RuntimeError('Native scroll handler returned no measurable result.')
                return outcome

            if pages == 10:
                # Dynamic feeds can grow when their old boundary is reached.
                # Re-measure and continue, with a hard cap, until the final
                # observed boundary is reached or scrolling makes no progress.
                for _ in range(10):
                    remaining = maximum - position if params.down else position
                    outcome = await dispatch_scroll(max(1, int(round(remaining))))
                    outcomes.append(outcome)
                    position = float(outcome['position'])
                    maximum = float(outcome['maximum'])
                    if outcome.get('at_boundary') or outcome.get('no_progress'):
                        break
            else:
                for amount in amounts:
                    outcome = await dispatch_scroll(amount)
                    outcomes.append(outcome)
                    if outcome.get('at_boundary') or outcome.get('no_progress'):
                        break

            final = outcomes[-1]
            moved = sum(int(item.get('moved', 0)) for item in outcomes)
            deliveries = list(dict.fromkeys(
                str(item['delivery']) for item in outcomes if item.get('delivery') != 'none'
            ))
            target = (
                f'element {params.index}'
                if params.index is not None and params.index != 0
                else 'page'
            )
            position = int(final['position'])
            maximum = int(final['maximum'])
            above = position
            below = max(0, maximum - position)
            if moved:
                memory = (
                    f'Scrolled {direction} {target} by {moved}px; '
                    f'{above}px above and {below}px below.'
                )
                if final.get('at_boundary'):
                    memory += f' Reached the {"bottom" if params.down else "top"} boundary.'
            elif final.get('at_boundary'):
                memory = f'No scroll movement: {target} is already at the {"bottom" if params.down else "top"} boundary.'
            else:
                memory = f'No scroll movement occurred in {target}; re-observe before retrying.'

            return ActionResult(
                extracted_content=f'🔍 {memory}',
                long_term_memory=memory,
                metadata={
                    'direction': direction,
                    'requested_pages': pages,
                    'moved_pixels': moved,
                    'position': position,
                    'maximum': maximum,
                    'pixels_above': above,
                    'pixels_below': below,
                    'at_boundary': bool(final.get('at_boundary')),
                    'no_progress': moved == 0,
                    'target': final.get('target'),
                    'delivery': deliveries[-1] if deliveries else 'none',
                    'deliveries': deliveries,
                    'attempts': len(outcomes),
                },
            )
        except Exception as exc:
            return ActionResult(error=f'Failed to execute native scroll: {type(exc).__name__}: {exc}')

    @tools.registry.action(
        'Query DOM elements by CSS selector. Returns tag, text, attributes, and visible geometry in the same screenshot coordinate space accepted by click and drag_and_drop.',
        param_model=FindElementsAction,
    )
    async def find_elements(params: FindElementsAction, browser_session: BrowserSession):
        session = _session(browser_session)
        page = await session.get_current_page()
        data = await page.evaluate(_build_find_elements_js(
            selector=params.selector,
            attributes=params.attributes,
            max_results=params.max_results,
            include_text=params.include_text,
        ))
        if isinstance(data, dict) and data.get('error'):
            return ActionResult(error=f"find_elements: {data['error']}")
        geometry = await page.evaluate(
            """({selector, limit}) => Array.from(document.querySelectorAll(selector))
                .slice(0, limit)
                .map(element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return {
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height,
                        visible: rect.width > 0 && rect.height > 0
                            && rect.right > 0 && rect.bottom > 0
                            && rect.left < innerWidth && rect.top < innerHeight
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                    };
                })""",
            {'selector': params.selector, 'limit': params.max_results},
        )
        total = data.get('total', 0) if isinstance(data, dict) else 0
        formatted = _format_find_results(data, params.selector)
        if geometry:
            lines = [formatted, '', 'Action-coordinate geometry:']
            for index, rect in enumerate(geometry):
                action_rect = _viewport_rect_to_image_coordinates(rect, session)
                lines.append(
                    f"[{index}] rect=({action_rect['x']}, {action_rect['y']}, "
                    f"{action_rect['width']}x{action_rect['height']}) "
                    f"center=({action_rect['center_x']}, {action_rect['center_y']}) "
                    f"visible={str(bool(rect['visible'])).lower()}"
                )
            formatted = '\n'.join(lines)
        return ActionResult(
            extracted_content=formatted,
            include_extracted_content_only_once=True,
            long_term_memory=f'Found {total} element(s) matching "{params.selector}".',
        )

    @tools.registry.action('Search page text for a pattern.', param_model=SearchPageAction)
    async def search_page(params: SearchPageAction, browser_session: BrowserSession):
        page = await _session(browser_session).get_current_page()
        data = await page.evaluate(_build_search_page_js(
            pattern=params.pattern,
            regex=params.regex,
            case_sensitive=params.case_sensitive,
            context_chars=params.context_chars,
            css_scope=params.css_scope,
            max_results=params.max_results,
        ))
        if isinstance(data, dict) and data.get('error'):
            return ActionResult(error=f"search_page: {data['error']}")
        total = data.get('total', 0) if isinstance(data, dict) else 0
        return ActionResult(
            extracted_content=_format_search_results(data, params.pattern),
            include_in_memory=True,
            long_term_memory=f'Found {total} match(es) for "{params.pattern}".',
        )

    @tools.registry.action(
        'Execute browser JavaScript. This ends the current action sequence; '
        'request any subsequent action on the next step. DOM query offsets are not Browser-Use element indices.',
        terminates_sequence=True,
    )
    async def evaluate(code: str, browser_session: BrowserSession):
        session = _session(browser_session)
        page = await session.get_current_page()
        try:
            try:
                result = await asyncio.wait_for(page.evaluate(code), EVALUATE_TIMEOUT_SECONDS)
            except TimeoutError:
                try:
                    await asyncio.wait_for(page.evaluate('() => 1'), 3)
                except Exception:
                    # The agent's own script holds the main thread; nothing else can run on this tab.
                    fresh = await session.session_manager.reload_unresponsive_page(page)
                    return ActionResult(error=(
                        f'JavaScript did not return within {EVALUATE_TIMEOUT_SECONDS:.0f}s and froze the page, so the '
                        f'tab was reloaded at {fresh.url}. Page state the site does not persist was lost. '
                        'Bound every loop in evaluate code.'
                    ))
                return ActionResult(error=(
                    f'JavaScript evaluation did not finish within {EVALUATE_TIMEOUT_SECONDS:.0f}s; '
                    'the page is still responsive.'
                ))
            output, truncated = _bounded_evaluate_output(result)
            memory = 'JavaScript evaluation succeeded.'
            if truncated:
                memory += f' Its output was windowed to {MAX_EVALUATE_OUTPUT_CHARS} characters.'
            return ActionResult(
                extracted_content=f'Executed script successfully: {output}',
                include_extracted_content_only_once=True,
                long_term_memory=memory,
            )
        except Exception as exc:
            return ActionResult(error=f'JavaScript evaluation failed: {exc}')

    @tools.registry.action(
        'Take a screenshot of the current viewport. Omit file_name to include the image in the next browser observation. '
        'With file_name, saves the image and returns its path instead.', param_model=ScreenshotAction,
    )
    async def screenshot(params: ScreenshotAction, browser_session: BrowserSession, file_system: FileSystem):
        session = _session(browser_session)
        if not params.file_name:
            return ActionResult(
                extracted_content='Requested screenshot for next observation',
                metadata={'include_screenshot': True},
            )
        name = _safe_output_name(params.file_name, '.png')
        if name is None:
            return ActionResult(error=f"Security error: Path traversal in file_name '{params.file_name}' is forbidden.")
        output = file_system.get_dir() / name
        output.write_bytes(await session.take_screenshot(full_page=False))
        return ActionResult(
            extracted_content=f'Screenshot saved to {name}',
            long_term_memory=f'Screenshot saved to {output}',
            attachments=[str(output)],
        )

    @tools.registry.action('Save the current page as a PDF file.', param_model=SaveAsPdfAction)
    async def save_as_pdf(params: SaveAsPdfAction, browser_session: BrowserSession, file_system: FileSystem):
        session = _session(browser_session)
        raw_name = getattr(params, 'file_name', None)
        if raw_name:
            name = _safe_output_name(raw_name, '.pdf')
            if name is None:
                return ActionResult(error=f"Security error: Path traversal in file_name '{raw_name}' is forbidden.")
        else:
            title = await (await session.get_current_page()).title() or 'page'
            name = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', title)[:30]}.pdf"
        output = file_system.get_dir() / name
        if output.exists():
            stem, suffix, counter = output.stem, output.suffix, 1
            while (output.parent / f'{stem} ({counter}){suffix}').exists():
                counter += 1
            output = output.parent / f'{stem} ({counter}){suffix}'

        paper_sizes = {
            'letter': (816, 1056), 'legal': (816, 1344), 'a4': (794, 1123),
            'a3': (1123, 1588), 'tabloid': (1056, 1632),
        }
        width, height = paper_sizes.get(
            (getattr(params, 'paper_format', 'Letter') or 'Letter').lower(),
            paper_sizes['letter'],
        )
        if getattr(params, 'landscape', False):
            width, height = height, width
        header_footer = bool(getattr(params, 'display_header_footer', False))
        if header_footer and (getattr(params, 'header_template', None) or getattr(params, 'footer_template', None)):
            # Firefox PDFs are rasterized here; Chromium print templates have no native equivalent.
            return ActionResult(error='Custom PDF header/footer templates are unsupported on Camoufox; use display_header_footer with the default metadata or disable it.')
        margin = 28 if header_footer else 0
        image = Image.open(io.BytesIO(await session.take_screenshot(full_page=True))).convert('RGB')
        scale = max(0.1, min(2.0, float(getattr(params, 'scale', 1.0) or 1.0)))
        if scale != 1.0:
            image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
        content_height = height - 2 * margin
        slice_height = max(1, int(image.width * content_height / width))
        pages = []
        for y in range(0, image.height, slice_height):
            page_image = Image.new('RGB', (image.width, slice_height), 'white')
            page_image.paste(image.crop((0, y, image.width, min(y + slice_height, image.height))))
            sheet = Image.new('RGB', (width, height), 'white')
            sheet.paste(page_image.resize((width, content_height), Image.Resampling.LANCZOS), (0, margin))
            pages.append(sheet)
        if header_footer:
            # Mirrors Browser-Use's default print metadata: date header, URL plus page numbers footer.
            page_url = (await session.get_current_page()).url
            today = datetime.now().strftime('%m/%d/%y, %I:%M %p')
            for number, sheet in enumerate(pages, start=1):
                draw = ImageDraw.Draw(sheet)
                counter = f'{number} / {len(pages)}'
                draw.text((width - 38 - draw.textlength(today), 9), today, fill='#666666')
                url_text = page_url if len(page_url) <= 90 else page_url[:87] + '...'
                draw.text((38, height - 19), url_text, fill='#666666')
                draw.text((width - 38 - draw.textlength(counter), height - 19), counter, fill='#666666')
        buffer = io.BytesIO()
        pages[0].save(buffer, format='PDF', save_all=True, append_images=pages[1:])
        output.write_bytes(buffer.getvalue())
        message = f'Saved page as PDF: {output.name} ({output.stat().st_size:,} bytes, {len(pages)} page(s))'
        return ActionResult(
            extracted_content=message,
            long_term_memory=f'{message}. Full path: {output}',
            attachments=[str(output)],
        )

    @tools.registry.action('Upload a file to a file input element.', param_model=UploadFileAction)
    async def upload_file(
        params: UploadFileAction,
        browser_session: BrowserSession,
        available_file_paths: list[str],
        file_system: FileSystem,
    ):
        session = _session(browser_session)
        raw_path = params.path
        resolved: str | None = None
        if raw_path in available_file_paths or raw_path in session.downloaded_files:
            resolved = raw_path
        elif session.is_local and file_system and file_system.get_dir() and (file_obj := file_system.get_file(raw_path)):
            base = Path(file_system.get_dir()).resolve()
            candidate = (base / file_obj.full_name).resolve()
            if not candidate.is_relative_to(base):
                return ActionResult(error=f'Upload of {raw_path!r} escapes FileSystem directory; refusing.')
            resolved = str(candidate)
        elif not session.is_local:
            resolved = raw_path
        if resolved is None:
            return ActionResult(error=f"File path '{raw_path}' is not permitted for upload.")
        path = Path(resolved).resolve()
        if not path.exists() or path.stat().st_size == 0:
            return ActionResult(error=f"File '{resolved}' does not exist or is empty.")
        node = await session.get_element_by_index(params.index)
        if node is None:
            return ActionResult(error=f'Element index {params.index} not found.')
        file_node = node if session.is_file_input(node) else session.find_file_input_near_element(node)
        if file_node is not None:
            await session.upload_file(file_node, str(path))
        else:
            control = session.locator_for_node(node)
            nearby = control.locator("xpath=ancestor-or-self::*[.//input[@type='file']][1]//input[@type='file']")
            candidates = nearby if await nearby.count() == 1 else (await session.get_current_page()).locator("input[type='file']")
            if await candidates.count() != 1:
                return ActionResult(error='Could not identify one unambiguous file input.')
            await candidates.set_input_files(str(path))
        return ActionResult(extracted_content=f'Uploaded {path.name} to element {params.index}')

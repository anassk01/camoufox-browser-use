"""Differential contract test: upstream Browser-Use Chrome vs Camoufox.

This intentionally avoids an LLM.  Both backends receive the same pages and
the same Browser-Use tool calls, so failures belong to browser integration
rather than model variance or a changing public website.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any, Iterator

# Differential runs must measure the browser backends, not analytics retries.
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'False')
os.environ.setdefault('BROWSER_USE_CLOUD_SYNC', 'False')

from browser_use import Browser
from browser_use.tools.service import Tools

from camoufox_browseruse import CamoufoxSession, register_camoufox_tools


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / 'tests'


class _QuietFileHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


class _RouteHandler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if self.path == '/clicked':
            self.__class__.hits.append(self.path)
            body = 'ok'
        else:
            body = self.routes.get(self.path, 'not found')
        payload = body.encode('utf-8')
        self.send_response(200 if self.path in self.routes or self.path == '/clicked' else 404)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _start_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@contextmanager
def fixture_servers() -> Iterator[tuple[str, str, type[_RouteHandler]]]:
    files = _start_server(partial(_QuietFileHandler, directory=str(FIXTURE_DIR)))

    inner_handler = type(
        'InnerHandler',
        (_RouteHandler,),
        {
            'routes': {
                '/inner': (
                    '<button id="deep-action" '
                    'onclick="this.textContent=\'Deep clicked\'; fetch(\'/clicked\')">'
                    'Deep cross-origin action</button>'
                ),
            },
            'hits': [],
        },
    )
    inner = _start_server(inner_handler)
    middle_handler = type(
        'MiddleHandler',
        (_RouteHandler,),
        {
            'routes': {
                '/middle': (
                    '<button id="middle-action">Middle action</button>'
                    f'<iframe src="http://127.0.0.1:{inner.server_port}/inner"></iframe>'
                ),
            },
            'hits': [],
        },
    )
    middle = _start_server(middle_handler)
    outer_handler = type(
        'OuterHandler',
        (_RouteHandler,),
        {
            'routes': {
                '/': (
                    '<button id="outer-action">Outer action</button>'
                    f'<iframe src="http://127.0.0.1:{middle.server_port}/middle"></iframe>'
                ),
            },
            'hits': [],
        },
    )
    outer = _start_server(outer_handler)
    try:
        yield (
            f'http://127.0.0.1:{files.server_port}/stress_fixture.html',
            f'http://127.0.0.1:{outer.server_port}/',
            inner_handler,
        )
    finally:
        for server in (outer, middle, inner, files):
            server.shutdown()
            server.server_close()


def create_session(backend: str, downloads_path: str):
    common = {
        'headless': True,
        'viewport': {'width': 1280, 'height': 900},
        'minimum_wait_page_load_time': 0,
        'wait_for_network_idle_page_load_time': 0,
        'wait_between_actions': 0,
        'keep_alive': True,
        'downloads_path': downloads_path,
        'cross_origin_iframes': True,
        'max_iframe_depth': 5,
    }
    if backend == 'camoufox':
        return CamoufoxSession(
            humanize=False,
            persistent_context=False,
            block_ip_addresses=False,
            **common,
        )
    return Browser(
        executable_path='/usr/bin/google-chrome',
        enable_default_extensions=False,
        **common,
    )


def _index_by_id(state: Any, element_id: str) -> int:
    for index, node in state.dom_state.selector_map.items():
        if (node.attributes or {}).get('id') == element_id:
            return index
    raise LookupError(f'No indexed element with id={element_id!r}')


def _decode_evaluate(value: Any) -> Any:
    """The upstream CDP Page wrapper returns JSON strings; Playwright does not."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


async def _evaluate(page: Any, expression: str) -> Any:
    return _decode_evaluate(await page.evaluate(expression))


async def _action(tools: Tools, session: Any, name: str, params: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    result = await tools.registry.execute_action(name, params, browser_session=session)
    return {
        'seconds': round(time.perf_counter() - started, 4),
        'error': getattr(result, 'error', None),
        'text': getattr(result, 'extracted_content', None),
        'metadata': getattr(result, 'metadata', None),
    }


async def _wait_for_tab_count(session: Any, count: int, timeout: float = 3.0) -> tuple[list[Any], float]:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        tabs = await session.get_tabs()
        if len(tabs) == count:
            return tabs, time.perf_counter() - started
        await asyncio.sleep(0.05)
    return await session.get_tabs(), time.perf_counter() - started


async def _wait_for_download(session: Any, timeout: float = 3.0) -> tuple[list[str], float]:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        files = list(session.downloaded_files)
        if files and all(Path(path).exists() for path in files):
            return files, time.perf_counter() - started
        await asyncio.sleep(0.05)
    return list(session.downloaded_files), time.perf_counter() - started


async def _observe_deep_frame(session: Any, timeout: float = 5.0) -> tuple[Any, float]:
    started = time.perf_counter()
    latest = None
    while time.perf_counter() - started < timeout:
        latest = await session.get_browser_state_summary(include_screenshot=False)
        try:
            _index_by_id(latest, 'deep-action')
            return latest, time.perf_counter() - started
        except LookupError:
            await asyncio.sleep(0.1)
    return latest, time.perf_counter() - started


async def exercise_backend(
    backend: str,
    fixture_url: str,
    cross_origin_url: str,
    inner_handler: type[_RouteHandler],
    iterations: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f'{backend}-differential-') as downloads_dir:
        session = create_session(backend, downloads_dir)
        tools = Tools()
        if backend == 'camoufox':
            register_camoufox_tools(tools)
        report: dict[str, Any] = {'backend': backend, 'actions': {}, 'checks': {}}
        started = time.perf_counter()
        await session.start()
        report['timing'] = {'startup_seconds': round(time.perf_counter() - started, 4)}
        try:
            started = time.perf_counter()
            await session.navigate_to(fixture_url)
            report['timing']['initial_navigation_seconds'] = round(time.perf_counter() - started, 4)

            initial = await session.get_browser_state_summary(include_screenshot=False)
            selector_map = initial.dom_state.selector_map
            report['dom'] = {
                'indexed_elements': len(selector_map),
                'indexed_ids': sorted({
                    element_id
                    for node in selector_map.values()
                    if (element_id := (node.attributes or {}).get('id'))
                }),
                'indexed_static_tags': [
                    node.tag_name
                    for node in selector_map.values()
                    if node.tag_name.lower() in {'h1', 'h2', 'h3', 'p', 'span'}
                ],
                'llm_representation_chars': len(initial.dom_state.llm_representation()),
            }

            state_samples = []
            screenshot_samples = []
            for _ in range(iterations):
                sample_started = time.perf_counter()
                await session.get_browser_state_summary(include_screenshot=False)
                state_samples.append(time.perf_counter() - sample_started)
                sample_started = time.perf_counter()
                image = await session.take_screenshot(full_page=False)
                screenshot_samples.append(time.perf_counter() - sample_started)
            report['timing'].update({
                'state_median_seconds': round(median(state_samples), 4),
                'screenshot_median_seconds': round(median(screenshot_samples), 4),
                'screenshot_bytes': len(image),
            })

            page = await session.get_current_page()
            report['engine'] = await _evaluate(
                page,
                '() => ({userAgent:navigator.userAgent, platform:navigator.platform})',
            )

            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['textarea_input'] = await _action(
                tools, session, 'input',
                {'index': _index_by_id(state, 'multi-area'), 'text': 'differential value'},
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['contenteditable_input'] = await _action(
                tools, session, 'input',
                {'index': _index_by_id(state, 'rich-editor'), 'text': 'edited content'},
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['select'] = await _action(
                tools, session, 'select_dropdown',
                {'index': _index_by_id(state, 'stress-select'), 'text': 'Option 37'},
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['checkbox'] = await _action(
                tools, session, 'click', {'index': _index_by_id(state, 'test-check')},
            )

            # Preserve a selector-map node, replace it in the live DOM, then use
            # the old index.  This is the failure mode seen on dynamic SPAs.
            state = await session.get_browser_state_summary(include_screenshot=False)
            stale_target = _index_by_id(state, 'shift-btn')
            replaced_target = next(
                index for index, node in state.dom_state.selector_map.items()
                if 'Target Node To Click' in node.get_meaningful_text_for_llm()
            )
            report['actions']['mutate_dom'] = await _action(
                tools, session, 'click', {'index': stale_target},
            )
            report['actions']['stale_click'] = await _action(
                tools, session, 'click', {'index': replaced_target},
            )

            values = await _evaluate(
                page,
                """() => ({
                    textarea: document.querySelector('#multi-area').value,
                    contenteditable: document.querySelector('#rich-editor').textContent,
                    select: document.querySelector('#stress-select').value,
                    checked: document.querySelector('#test-check').checked,
                    staleStatus: document.querySelector('#action-status').textContent
                })""",
            )
            report['form_values'] = values
            report['checks']['form_controls'] = values == {
                'textarea': 'differential value',
                'contenteditable': 'edited content',
                'select': 'opt_37',
                'checked': True,
                'staleStatus': 'Action Succeeded!',
            }
            report['checks']['stale_recovery'] = values.get('staleStatus') == 'Action Succeeded!'

            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['shadow_click'] = await _action(
                tools, session, 'click', {'index': _index_by_id(state, 'shadow-btn')},
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['iframe_input'] = await _action(
                tools, session, 'input',
                {'index': _index_by_id(state, 'iframe-input'), 'text': 'frame value'},
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['iframe_click'] = await _action(
                tools, session, 'click', {'index': _index_by_id(state, 'iframe-btn')},
            )
            nested = await _evaluate(
                page,
                """() => ({
                    shadow: document.querySelector('#shadow-host').shadowRoot
                        .querySelector('#shadow-btn').textContent,
                    iframeInput: document.querySelector('#inner-frame').contentDocument
                        .querySelector('#iframe-input').value,
                    iframeStatus: document.querySelector('#inner-frame').contentDocument
                        .querySelector('#iframe-status').textContent
                })""",
            )
            report['nested_values'] = nested
            report['checks']['open_shadow_dom'] = nested.get('shadow') == 'Shadow Clicked!'
            report['checks']['same_origin_iframe'] = nested.get('iframeInput') == 'frame value' \
                and nested.get('iframeStatus') == 'Iframe Clicked!'

            report['actions']['search_page'] = await _action(
                tools, session, 'search_page',
                {
                    'pattern': 'Automation Stress Test', 'regex': False,
                    'case_sensitive': False, 'context_chars': 30, 'max_results': 10,
                },
            )
            report['actions']['find_elements'] = await _action(
                tools, session, 'find_elements',
                {
                    'selector': 'button', 'attributes': ['id'],
                    'max_results': 30, 'include_text': True,
                },
            )
            report['actions']['evaluate'] = await _action(
                tools, session, 'evaluate', {'code': '() => document.title'},
            )
            report['checks']['search_page'] = '1 match' in (report['actions']['search_page']['text'] or '')
            report['checks']['find_elements'] = '10 elements' in (report['actions']['find_elements']['text'] or '')
            report['checks']['evaluate_title'] = 'Camoufox Stress & Parity Fixture' in (
                report['actions']['evaluate']['text'] or ''
            )

            await page.evaluate(
                """() => {
                    const anchor = document.createElement('a');
                    anchor.id = 'popup-link';
                    anchor.textContent = 'Open child tab';
                    anchor.href = location.href + '?child=1';
                    anchor.target = '_blank';
                    document.body.prepend(anchor);
                }"""
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['popup_click'] = await _action(
                tools, session, 'click', {'index': _index_by_id(state, 'popup-link')},
            )
            tabs, popup_settle = await _wait_for_tab_count(session, 2)
            report['tabs'] = {
                'count_after_click': len(tabs),
                'settle_seconds': round(popup_settle, 4),
                'reported_by_click': 'Automatically switched' in (
                    report['actions']['popup_click']['text'] or ''
                ),
                'urls': [tab.url for tab in tabs],
            }
            report['checks']['new_tab'] = len(tabs) == 2 and any('?child=1' in tab.url for tab in tabs)
            if len(tabs) > 1:
                report['actions']['close_tab'] = await _action(
                    tools, session, 'close', {'tab_id': tabs[-1].target_id[-4:]},
                )
                await _wait_for_tab_count(session, 1)
                await asyncio.sleep(0.3)

            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['dialogs'] = await _action(
                tools, session, 'click', {'index': _index_by_id(state, 'trigger-dialogs-btn')},
            )
            await asyncio.sleep(0.1)
            dialog_state = await session.get_browser_state_summary(include_screenshot=False)
            report['dialogs'] = list(dialog_state.closed_popup_messages)
            report['checks']['dialogs'] = {
                '[alert] Dialog 1', '[alert] Dialog 2', '[confirm] Confirm 3',
            }.issubset(set(report['dialogs']))

            await page.evaluate(
                """() => {
                    const anchor = document.createElement('a');
                    anchor.id = 'download-link';
                    anchor.textContent = 'Download fixture';
                    anchor.href = '/test_downloads/browseruse_agent_data/suite_test.pdf';
                    anchor.download = 'suite_test.pdf';
                    document.body.prepend(anchor);
                }"""
            )
            state = await session.get_browser_state_summary(include_screenshot=False)
            report['actions']['download'] = await _action(
                tools, session, 'click', {'index': _index_by_id(state, 'download-link')},
            )
            downloads, download_settle = await _wait_for_download(session)
            report['download'] = {
                'settle_seconds': round(download_settle, 4),
                'files': [Path(path).name for path in downloads],
                'sizes': [Path(path).stat().st_size for path in downloads if Path(path).exists()],
            }
            report['checks']['download'] = bool(report['download']['sizes']) \
                and all(size > 0 for size in report['download']['sizes'])

            inner_handler.hits.clear()
            started = time.perf_counter()
            await session.navigate_to(cross_origin_url)
            report['timing']['cross_origin_navigation_seconds'] = round(time.perf_counter() - started, 4)
            deep_state, deep_settle = await _observe_deep_frame(session)
            deep_index = None
            try:
                deep_index = _index_by_id(deep_state, 'deep-action')
            except (LookupError, AttributeError):
                pass
            report['cross_origin'] = {
                'perception_seconds': round(deep_settle, 4),
                'deep_index': deep_index,
            }
            if deep_index is not None:
                deep_node = deep_state.dom_state.selector_map[deep_index]
                report['cross_origin'].update({
                    'frame_id': deep_node.frame_id,
                    'frame_depth': (deep_node.attributes or {}).get(
                        'data-browser-use-camoufox-frame-depth'
                    ),
                })
                report['actions']['cross_origin_click'] = await _action(
                    tools, session, 'click', {'index': deep_index},
                )
                for _ in range(30):
                    if inner_handler.hits:
                        break
                    await asyncio.sleep(0.1)
            report['cross_origin']['server_received_click'] = bool(inner_handler.hits)
            report['checks']['nested_cross_origin_iframe'] = deep_index is not None \
                and bool(inner_handler.hits)
            report['checks']['screenshot'] = report['timing']['screenshot_bytes'] > 1000
            return report
        except Exception as exc:
            report['fatal_error'] = f'{type(exc).__name__}: {exc}'
            return report
        finally:
            await session.kill()


def compare(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {result['backend']: result for result in results}
    original = by_name['original']
    camoufox = by_name['camoufox']
    all_checks = sorted(set(original.get('checks', {})) | set(camoufox.get('checks', {})))
    matrix = {
        check: {
            'original': bool(original.get('checks', {}).get(check)),
            'camoufox': bool(camoufox.get('checks', {}).get(check)),
        }
        for check in all_checks
    }
    return {
        'check_matrix': matrix,
        'camoufox_regressions': [
            check for check, outcome in matrix.items()
            if outcome['original'] and not outcome['camoufox']
        ],
        'camoufox_advantages': [
            check for check, outcome in matrix.items()
            if outcome['camoufox'] and not outcome['original']
        ],
        'structural_differences': {
            'indexed_elements': {
                'original': original.get('dom', {}).get('indexed_elements'),
                'camoufox': camoufox.get('dom', {}).get('indexed_elements'),
            },
            'indexed_static_tags': {
                'original': original.get('dom', {}).get('indexed_static_tags'),
                'camoufox': camoufox.get('dom', {}).get('indexed_static_tags'),
            },
            'popup_reported_in_click_result': {
                'original': original.get('tabs', {}).get('reported_by_click'),
                'camoufox': camoufox.get('tabs', {}).get('reported_by_click'),
            },
            'popup_settle_seconds': {
                'original': original.get('tabs', {}).get('settle_seconds'),
                'camoufox': camoufox.get('tabs', {}).get('settle_seconds'),
            },
            'dialog_notifications': {
                'original': len(original.get('dialogs', [])),
                'camoufox': len(camoufox.get('dialogs', [])),
            },
            'llm_representation_chars': {
                'original': original.get('dom', {}).get('llm_representation_chars'),
                'camoufox': camoufox.get('dom', {}).get('llm_representation_chars'),
            },
        },
    }


async def async_main(iterations: int) -> dict[str, Any]:
    with fixture_servers() as (fixture_url, cross_origin_url, inner_handler):
        results = []
        for backend in ('original', 'camoufox'):
            print(f'DIFFERENTIAL backend={backend}', flush=True)
            result = await exercise_backend(
                backend, fixture_url, cross_origin_url, inner_handler, iterations,
            )
            results.append(result)
            print(json.dumps(result.get('checks', {}), indent=2), flush=True)
        return {
            'iterations': iterations,
            'results': results,
            'comparison': compare(results),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--iterations', type=int, default=5)
    parser.add_argument(
        '--output', type=Path, default=Path('/tmp/camoufox-backend-differential.json'),
    )
    parser.add_argument(
        '--strict', action='store_true',
        help='Exit non-zero when upstream passes a check that Camoufox fails.',
    )
    args = parser.parse_args()
    report = asyncio.run(async_main(max(1, args.iterations)))
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    print(json.dumps(report['comparison'], indent=2), flush=True)
    print(f'REPORT={args.output}', flush=True)
    if args.strict and report['comparison']['camoufox_regressions']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

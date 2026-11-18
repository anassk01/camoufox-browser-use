"""Hard DOM parity tests using a three-origin nested frame tree."""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from camoufox_browseruse import open_camoufox_session


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        payload = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _server(routes: dict[str, str]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    handler = type('FixtureHandler', (_Handler,), {'routes': routes})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class TestNestedCrossOriginIframeParity(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        inner_html = """<!doctype html><html><body style="margin:11px">
            <button id="deep-button" onclick="document.querySelector('#deep-status').textContent='clicked'">
                Confirm embedded payment
            </button>
            <span id="deep-status">ready</span>
            <nested-host id="outer-shadow"></nested-host>
            <script>
                const outer = document.querySelector('#outer-shadow').attachShadow({mode: 'open'});
                outer.innerHTML = '<inner-host></inner-host>';
                const inner = outer.querySelector('inner-host').attachShadow({mode: 'open'});
                inner.innerHTML = '<button id="nested-shadow-button">Nested shadow action</button>';
            </script>
        </body></html>"""
        cls.inner_server, cls.inner_thread = _server({'/inner': inner_html})
        inner_port = cls.inner_server.server_address[1]

        middle_html = f"""<!doctype html><html><body style="margin:13px">
            <button id="middle-button">Middle frame action</button>
            <iframe id="deep-frame" style="margin:17px;border:5px solid black;width:360px;height:180px"
                src="http://127.0.0.1:{inner_port}/inner"></iframe>
        </body></html>"""
        cls.middle_server, cls.middle_thread = _server({'/middle': middle_html})
        middle_port = cls.middle_server.server_address[1]

        outer_html = f"""<!doctype html><html><body style="margin:19px">
            <button id="main-button">Main action</button>
            <div style="position:relative;width:220px;height:40px">
                <button id="covered-button">Covered action</button>
                <div id="cover" style="position:absolute;inset:0;background:white;z-index:5"></div>
            </div>
            <iframe id="middle-frame" style="display:block;margin:23px;border:7px solid black;width:500px;height:300px"
                src="http://127.0.0.1:{middle_port}/middle"></iframe>
            <iframe style="display:none" src="http://127.0.0.1:{inner_port}/inner"></iframe>
        </body></html>"""
        cls.outer_server, cls.outer_thread = _server({'/': outer_html})
        cls.url = f'http://127.0.0.1:{cls.outer_server.server_address[1]}/'

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.outer_server, cls.middle_server, cls.inner_server):
            server.shutdown()
            server.server_close()
        for thread in (cls.outer_thread, cls.middle_thread, cls.inner_thread):
            thread.join(timeout=2)

    async def test_nested_cross_origin_extract_click_coordinates_and_stable_identity(self) -> None:
        async with open_camoufox_session(
            headless=True,
            humanize=False,
            cross_origin_iframes=True,
            max_iframe_depth=5,
            max_iframes=10,
            paint_order_filtering=True,
        ) as session:
            await session.navigate_to(self.url)
            page = await session.get_current_page()
            await page.frame_locator('#middle-frame').frame_locator('#deep-frame').locator('#deep-button').wait_for()

            first = await session.get_dom_state()
            self.assertFalse(any(
                (candidate.attributes or {}).get('id') == 'covered-button'
                for candidate in first.selector_map.values()
            ), 'paint-order filtering should remove an occluded control')
            matches = [
                (index, node) for index, node in first.selector_map.items()
                if (node.attributes or {}).get('id') == 'deep-button'
            ]
            self.assertEqual(len(matches), 1, 'deep cross-origin target should be extracted once')
            index, node = matches[0]
            attrs = node.attributes or {}
            self.assertEqual(attrs['data-browser-use-camoufox-frame-depth'], '2')
            frame_id = attrs['data-browser-use-camoufox-frame']
            self.assertNotEqual(frame_id, 'main')
            self.assertEqual(node.parent_node.node_name.lower(), 'iframe')
            self.assertEqual(
                (node.parent_node.attributes or {}).get('data-browser-use-camoufox-child-frame'),
                frame_id,
            )
            self.assertEqual(node.parent_node.parent_node.node_name.lower(), 'iframe')
            self.assertEqual(node.parent_node.parent_node.parent_node.node_name, '#document')

            deep_frame = next(
                frame for frame in page.frames
                if frame.url.endswith('/inner')
                and frame.parent_frame is not None
                and frame.parent_frame.url.endswith('/middle')
                and not frame.is_detached()
            )
            actual_box = await deep_frame.locator('#deep-button').bounding_box()
            self.assertIsNotNone(actual_box)
            self.assertAlmostEqual(node.absolute_position.x, actual_box['x'], delta=1.0)
            self.assertAlmostEqual(node.absolute_position.y, actual_box['y'], delta=1.0)

            await session.click_action(index)
            self.assertEqual(await deep_frame.locator('#deep-status').inner_text(), 'clicked')

            # Adding a sibling changes page.frames ordering. The existing target
            # must retain its ID because actions may outlive any single snapshot.
            await page.evaluate("""() => {
                const frame = document.createElement('iframe');
                frame.src = 'about:blank';
                document.body.prepend(frame);
            }""")
            await page.wait_for_timeout(100)
            second = await session.get_dom_state()
            second_node = next(
                node for node in second.selector_map.values()
                if (node.attributes or {}).get('id') == 'deep-button'
            )
            self.assertEqual(
                (second_node.attributes or {})['data-browser-use-camoufox-frame'],
                frame_id,
            )

            nested_shadow = next(
                (idx, candidate) for idx, candidate in second.selector_map.items()
                if (candidate.attributes or {}).get('id') == 'nested-shadow-button'
            )
            self.assertEqual(nested_shadow[1].parent_node.node_name.lower(), 'inner-host')
            self.assertEqual(nested_shadow[1].parent_node.parent_node.node_name.lower(), 'nested-host')
            await session.click_action(nested_shadow[0])
            self.assertEqual(await deep_frame.locator('#nested-shadow-button').count(), 1)

            # Replace the entire cross-origin subtree. Clicking the old index
            # must refresh and relocalize instead of falling back to main frame.
            old_deep_index = next(
                idx for idx, candidate in second.selector_map.items()
                if (candidate.attributes or {}).get('id') == 'deep-button'
            )
            await page.evaluate("""() => {
                const oldFrame = document.querySelector('#middle-frame');
                oldFrame.replaceWith(oldFrame.cloneNode());
            }""")
            deep_button = page.frame_locator('#middle-frame').frame_locator('#deep-frame').locator('#deep-button')
            await deep_button.wait_for()
            await session.click_action(old_deep_index)
            self.assertEqual(
                await page.frame_locator('#middle-frame').frame_locator('#deep-frame').locator('#deep-status').inner_text(),
                'clicked',
            )

    async def test_profile_disables_cross_origin_subtree(self) -> None:
        async with open_camoufox_session(
            headless=True,
            humanize=False,
            cross_origin_iframes=False,
            paint_order_filtering=False,
        ) as session:
            await session.navigate_to(self.url)
            state = await session.get_dom_state()
            ids = {(node.attributes or {}).get('id') for node in state.selector_map.values()}
            self.assertIn('main-button', ids)
            self.assertIn('covered-button', ids, 'disabling paint filtering should retain covered controls')
            self.assertNotIn('middle-button', ids)
            self.assertNotIn('deep-button', ids)

    async def test_profile_enforces_depth_limit(self) -> None:
        async with open_camoufox_session(
            headless=True,
            humanize=False,
            cross_origin_iframes=True,
            max_iframe_depth=1,
        ) as session:
            await session.navigate_to(self.url)
            state = await session.get_dom_state()
            ids = {(node.attributes or {}).get('id') for node in state.selector_map.values()}
            self.assertIn('middle-button', ids)
            self.assertNotIn('deep-button', ids)


if __name__ == '__main__':
    unittest.main()

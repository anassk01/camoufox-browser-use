"""Browser-Use 0.13 observation contracts implemented natively on Camoufox."""

import asyncio
import base64
import os
import tempfile
import time
import unittest
from io import BytesIO
from unittest import mock

from PIL import Image
from browser_use import Tools
from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.browser.views import BrowserError
from browser_use.filesystem.file_system import FileSystem

from camoufox_browseruse import CamoufoxSession, open_camoufox_session, register_camoufox_tools

FORM = """
<input id="card" autocomplete="cc-number">
<input id="otp" autocomplete="one-time-code">
<input id="token" type="hidden" value="hidden-token-value">
<input id="nick">
<input id="agree" type="checkbox" checked>
<a href="/home" id="logo"><img src="/static/brand-logo.png" alt="Acme home"></a>
<script>
  document.getElementById('card').value = '4111111111111111';
  document.getElementById('otp').value = '424242';
  document.getElementById('nick').value = 'prefilled-nick';
  document.getElementById('agree').checked = false;
</script>
"""

MODAL = """
<main><p id="behind">Text hidden behind the modal</p></main>
<div style="position:fixed;inset:0;background:#fff;z-index:10"><p>Modal says hello</p></div>
"""


class UpstreamParityTests(unittest.IsolatedAsyncioTestCase):
    async def _serve(self, session, html: str) -> None:
        page = await session.get_current_page()
        await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
        await page.goto('https://fixture.test/')

    async def test_live_values_redact_secrets_and_reflect_checked_state(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, FORM)
            text = (await session.get_browser_state_summary(include_screenshot=False)).dom_state.llm_representation()
            self.assertIn('prefilled-nick', text)
            for secret in ('4111111111111111', '424242', 'hidden-token-value'):
                self.assertNotIn(secret, text)
            agree = await session.get_element_by_index(await session.get_index_by_id('agree'))
            self.assertNotIn('checked', agree.attributes)
            self.assertIn('image_alt=Acme home', text)
            self.assertIn('image_src=brand-logo.png', text)

    async def test_text_painted_under_modal_is_not_observed(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, MODAL)
            text = (await session.get_browser_state_summary(include_screenshot=False)).dom_state.llm_representation()
            self.assertIn('Modal says hello', text)
            self.assertNotIn('Text hidden behind the modal', text)

    async def test_pending_requests_drive_loading_hint(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            release = asyncio.Event()

            async def handler(route):
                if route.request.url.endswith('/slow.json'):
                    await release.wait()
                    await route.fulfill(body='{}')
                else:
                    await route.fulfill(content_type='text/html', body="<p>Loading</p><script>fetch('/slow.json')</script>")

            await page.route('**/*', handler)
            async with page.expect_request('**/slow.json'):
                await page.goto('https://fixture.test/')
            state = await session.get_browser_state_summary(include_screenshot=False)
            self.assertEqual([request.url for request in state.pending_network_requests], ['https://fixture.test/slow.json'])
            async with page.expect_event('requestfinished', lambda request: request.url.endswith('/slow.json')):
                release.set()
            state = await session.get_browser_state_summary(include_screenshot=False)
            self.assertEqual(state.pending_network_requests, [])

    async def test_screenshot_failure_keeps_dom_state(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, '<button>Still here</button>')
            with mock.patch.object(type(session), 'take_screenshot', side_effect=RuntimeError('stalled')):
                state = await session.get_browser_state_summary()
            self.assertIsNone(state.screenshot)
            self.assertIn('Still here', state.dom_state.llm_representation())

    async def test_state_timeout_returns_non_actionable_state(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, '<button>Old</button>')
            await session.get_browser_state_summary(include_screenshot=False)
            self.assertTrue(session.dom_watchdog.selector_map)

            async def hang():
                await asyncio.sleep(30)

            with mock.patch.dict(os.environ, {'TIMEOUT_BrowserStateRequestEvent': '1'}), \
                    mock.patch.object(session.dom_watchdog, 'get_state', side_effect=hang):
                state = await session.get_browser_state_summary(include_screenshot=False)
            self.assertIn('timed out', state.state_error)
            self.assertEqual(state.dom_state.selector_map, {})
            self.assertEqual(session.dom_watchdog.selector_map, {})
            prompt = AgentMessagePrompt(browser_state_summary=state, file_system=None)
            self.assertIn('<browser_state_error>', prompt._get_browser_state_description())

    async def test_pdf_prints_default_metadata_and_rejects_templates(self):
        tools = Tools()
        register_camoufox_tools(tools)
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, '<h1>Printable</h1>')
            with tempfile.TemporaryDirectory() as directory:
                file_system = FileSystem(directory)
                run = lambda **params: tools.registry.execute_action(
                    'save_as_pdf', params, browser_session=session, file_system=file_system)
                plain = await run(file_name='plain', display_header_footer=False)
                printed = await run(file_name='printed')
                self.assertIsNone(printed.error)
                # Header/footer metadata adds drawn text, so the raster PDF grows.
                sizes = [os.path.getsize(result.attachments[0]) for result in (plain, printed)]
                self.assertGreater(sizes[1], sizes[0])
                rejected = await run(file_name='custom', header_template='<span class="title"></span>')
                self.assertIn('unsupported', rejected.error)

    async def test_screenshot_labels_text_less_elements_as_the_agent_prompt_promises(self):
        tiles = ''.join(
            f'<div onclick="1" style="position:absolute;left:{40 + i * 100}px;top:40px;width:80px;height:80px;'
            f'background:#ddd;cursor:pointer"></div>' for i in range(3))
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, tiles + '<button style="position:absolute;top:300px">Verify</button>')

            async def tile_band(highlight):
                session.browser_profile.highlight_elements = highlight
                state = await session.get_browser_state_summary()
                with Image.open(BytesIO(base64.b64decode(state.screenshot))) as image:
                    ratio = image.width / state.page_info.viewport_width
                    # Upstream draws the index at the top centre of a text-less element.
                    return image.convert('RGB').crop(tuple(int(v * ratio) for v in (40, 40, 320, 70))).tobytes()

            self.assertNotEqual(await tile_band(True), await tile_band(False))

    def test_labels_stay_inside_small_elements_and_keep_their_state_visible(self):
        from browser_use.dom.views import DOMRect
        from camoufox_browseruse.watchdogs.screenshots import label_screenshot
        page = Image.new('RGB', (300, 200), 'white')
        page.paste((72, 141, 221), (100, 100, 128, 128))  # a selected 28 px tile
        buffer = BytesIO()
        page.save(buffer, format='PNG')
        tile = mock.Mock(absolute_position=DOMRect(x=100, y=100, width=28, height=28))
        tile.get_meaningful_text_for_llm.return_value = ''
        labeled = Image.open(BytesIO(label_screenshot(buffer.getvalue(), {12: tile}, 300))).convert('RGB')
        changed = [(x, y) for x in range(300) for y in range(200) if labeled.getpixel((x, y)) != page.getpixel((x, y))]
        self.assertTrue(changed)
        self.assertTrue(all(100 <= x < 128 and 100 <= y < 128 for x, y in changed))
        # Most of the tile still shows its selection colour.
        self.assertLess(len(changed), 28 * 28 * 0.4)

    def test_cloud_browsers_fail_explicitly(self):
        # A silently accepted cloud flag would make the local browser look remote.
        for options in ({'use_cloud': True}, {'cloud_browser': True}):
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                CamoufoxSession(headless=True, **options)

    def test_headless_follows_profile_and_environment(self):
        from browser_use.browser.profile import BrowserProfile
        with mock.patch.dict(os.environ, {'BROWSER_USE_HEADLESS': '1'}):
            self.assertIs(CamoufoxSession()._launch_options['headless'], True)
            self.assertIs(CamoufoxSession(headless=False)._launch_options['headless'], False)
        profile = BrowserProfile(headless=True)
        self.assertIs(CamoufoxSession(browser_profile=profile)._launch_options['headless'], True)

    async def test_same_document_navigation_and_failed_tab_switch(self):
        tools = Tools()
        register_camoufox_tools(tools)
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await self._serve(session, '<h1 id="top">Top</h1>')
            started = time.monotonic()
            result = await tools.registry.execute_action(
                'navigate', {'url': 'https://fixture.test/#section'}, browser_session=session)
            # Fragment navigations commit without a new load; they must not wait out a timeout.
            self.assertIsNone(result.error)
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue((await session.get_current_page_url()).endswith('#section'))
            # Upstream 0.13 reports failed switches as errors instead of silent success.
            with self.assertRaisesRegex(BrowserError, 'Failed to switch to tab'):
                await tools.registry.execute_action('switch', {'tab_id': 'zzzz'}, browser_session=session)


if __name__ == '__main__':
    unittest.main()

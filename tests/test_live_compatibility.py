"""Opt-in compatibility checks against public websites.

Run with CAMOUFOX_RUN_LIVE_TESTS=1. These tests are deliberately excluded from
the deterministic default suite because public sites and networks can fail.
"""

import os
import unittest
from urllib.parse import urlsplit

from browser_use.browser.events import ClickElementEvent, GoBackEvent, TypeTextEvent

from camoufox_browseruse import open_camoufox_session


@unittest.skipUnless(
    os.environ.get('CAMOUFOX_RUN_LIVE_TESTS') == '1',
    'set CAMOUFOX_RUN_LIVE_TESTS=1 to run public-site checks',
)
class TestLiveCompatibility(unittest.IsolatedAsyncioTestCase):
    async def test_https_navigation_dom_and_screenshot(self):
        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            await session.navigate_to('https://example.com')
            self.assertEqual(urlsplit(await session.get_current_page_url()).hostname, 'example.com')
            state = await session.get_browser_state_summary(include_screenshot=True)
            self.assertIn('Example Domain', state.title)
            self.assertIn('Example Domain', state.dom_state.llm_representation())
            self.assertGreater(len(state.screenshot or ''), 100)

    async def test_redirect_history_and_real_form(self):
        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            await session.navigate_to('https://httpbin.org/redirect/2')
            self.assertTrue((await session.get_current_page_url()).endswith('/get'))

            await session.navigate_to('https://httpbin.org/forms/post')
            page = await session.get_current_page()
            state = await session.get_dom_state()
            name_node = next(
                node
                for node in state.selector_map.values()
                if (node.attributes or {}).get('name') == 'custname'
            )
            submit_node = next(
                node
                for node in state.selector_map.values()
                if node.node_name.upper() == 'BUTTON'
            )
            type_event = session.event_bus.dispatch(TypeTextEvent(
                node=name_node,
                text='Camoufox parity',
                clear=True,
            ))
            await type_event
            await type_event.event_result(raise_if_any=True)
            await page.locator('form').evaluate("form => form.addEventListener('submit', event => event.preventDefault())")
            click_event = session.event_bus.dispatch(ClickElementEvent(node=submit_node))
            await click_event
            await click_event.event_result(raise_if_any=True)
            self.assertEqual(await page.locator('input[name="custname"]').input_value(), 'Camoufox parity')

            event = session.event_bus.dispatch(GoBackEvent())
            await event
            await event.event_result(raise_if_any=True)
            self.assertTrue((await session.get_current_page_url()).endswith('/get'))


if __name__ == '__main__':
    unittest.main()

"""An agent script that never returns must not end the session."""

import unittest
from unittest import mock

from browser_use import Tools

from camoufox_browseruse import open_camoufox_session, register_camoufox_tools
from camoufox_browseruse.tools import overrides

PAGE = '<title>Fixture</title><script>localStorage.setItem("level", "5")</script><p>hello</p>'


class EvaluateRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = Tools()
        register_camoufox_tools(self.tools)

    async def run_js(self, session, code):
        return await self.tools.registry.execute_action('evaluate', {'code': code}, browser_session=session)

    async def test_frozen_page_is_reloaded_and_slow_async_script_is_not(self):
        with mock.patch.object(overrides, 'EVALUATE_TIMEOUT_SECONDS', 2.0):
            async with open_camoufox_session(headless=True, humanize=False) as session:
                page = await session.get_current_page()
                await page.context.route('https://fixture.test/**', lambda route: route.fulfill(
                    content_type='text/html', body=PAGE))
                await page.goto('https://fixture.test/level')

                slow = await self.run_js(session, '() => new Promise(resolve => setTimeout(resolve, 10000))')
                self.assertIn('still responsive', slow.error)
                self.assertIs(await session.get_current_page(), page)

                frozen = await self.run_js(session, '() => { let node = document.body; while (node) {} }')
                self.assertIn('froze the page', frozen.error)
                fresh = await session.get_current_page()
                self.assertIsNot(fresh, page)
                self.assertTrue(page.is_closed())
                self.assertEqual(fresh.url, 'https://fixture.test/level')
                # The agent can continue on the same site; persisted site state survives.
                after = await self.run_js(session, '() => localStorage.getItem("level") + document.title')
                self.assertIsNone(after.error)
                self.assertIn('5Fixture', after.extracted_content)
                self.assertEqual(len(page.context.pages), 1)


if __name__ == '__main__':
    unittest.main()

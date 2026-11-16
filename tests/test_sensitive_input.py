"""Native targets support Browser-Use's domain-scoped sensitive input."""

import unittest

from browser_use import Tools
from camoufox_browseruse import open_camoufox_session, register_camoufox_tools


class SensitiveInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_lookup_tracks_navigation_and_scopes_secret_substitution(self):
        secret = 'FixturePassword1!'
        sensitive_data = {'https://accounts.example': {'fixture_password': secret}}
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.route('**/*', lambda route: route.fulfill(
                content_type='text/html', body='<label for="password">Password</label>'
                '<input id="password" type="password">'))
            tools = Tools()
            register_camoufox_tools(tools)
            for host, expected in (('accounts.example', secret),
                                   ('accounts.example.evil.example', '<secret>fixture_password</secret>')):
                await page.goto(f'https://{host}/login')
                await session.get_browser_state_summary()
                target = session.session_manager.get_target(session.agent_focus_target_id)
                self.assertEqual(target.url, page.url)
                self.assertEqual(target.target_id, session.agent_focus_target_id)
                index = await session.get_index_by_id('password')
                result = await tools.registry.execute_action(
                    'input', {'index': index, 'text': '<secret>fixture_password</secret>'},
                    browser_session=session, sensitive_data=sensitive_data)
                self.assertIsNone(result.error)
                self.assertEqual(await page.locator('#password').input_value(), expected)
                self.assertNotIn(secret, result.model_dump_json())
                state = await session.get_browser_state_summary()
                self.assertNotIn(secret, state.dom_state.llm_representation())
            self.assertIsNone(session.session_manager.get_target('missing-target'))

"""Native keyboard targeting added by Browser-Use 0.13.13."""

import unittest

from browser_use.browser.events import SendKeysEvent
from camoufox_browseruse import open_camoufox_session


class KeyboardTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_targeted_keys_change_only_requested_tab(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            manager = session.session_manager
            first = await session.get_current_page()
            await first.set_content('<input id="answer">')
            await first.locator('#answer').focus()
            context = await manager.ensure_context()
            second = await context.new_page()
            await second.set_content('<input id="answer">')
            await second.locator('#answer').focus()
            first_id = manager.target_id(first)
            second_id = manager.target_id(second)
            await manager.switch(first_id)

            event = session.event_bus.dispatch(SendKeysEvent(keys='x', target_id=second_id))
            await event.event_result(raise_if_any=True)
            self.assertEqual(await first.locator('#answer').input_value(), '')
            self.assertEqual(await second.locator('#answer').input_value(), 'x')
            self.assertEqual(session.agent_focus_target_id, second_id)

            event = session.event_bus.dispatch(SendKeysEvent(keys='y'))
            await event.event_result(raise_if_any=True)
            self.assertEqual(await second.locator('#answer').input_value(), 'xy')

            event = session.event_bus.dispatch(SendKeysEvent(keys='z', target_id='missing-tab'))
            with self.assertRaises(Exception):
                await event.event_result(raise_if_any=True)
            self.assertEqual(await second.locator('#answer').input_value(), 'xy')

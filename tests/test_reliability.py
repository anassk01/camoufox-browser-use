"""Lifecycle and recovery tests that exercise repeated browser ownership."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from browser_use.browser.events import BrowserErrorEvent, TargetCrashedEvent

from camoufox_browseruse import CamoufoxSession


class TestNativeReliability(unittest.IsolatedAsyncioTestCase):
    async def test_same_session_restarts_without_stale_state_or_monitor(self):
        session = CamoufoxSession(headless=True, humanize=False, persistent_context=False)
        try:
            for cycle in range(3):
                await session.start()
                page = await session.get_current_page()
                await page.set_content(f'<button id="cycle">cycle-{cycle}</button>')
                state = await session.get_dom_state()
                self.assertIn(f'cycle-{cycle}', state.llm_representation())
                self.assertTrue(session.crash_watchdog.is_monitoring)

                await session.stop()
                self.assertIsNone(session.session_manager.context)
                self.assertIsNone(session.session_manager.camoufox)
                self.assertFalse(session.crash_watchdog.is_monitoring)
                self.assertEqual(session.dom_watchdog.selector_map, {})
        finally:
            await session.stop()

    async def test_target_crash_recovers_focus_and_reports_error(self):
        session = CamoufoxSession(headless=True, humanize=False, persistent_context=False)
        errors = []
        session.event_bus.on(BrowserErrorEvent, lambda event: errors.append(event))
        try:
            await session.start()
            crashed_page = await session.get_current_page()
            crashed_target = session.session_manager.target_id(crashed_page)
            backup_page = await session.new_page('about:blank')
            await backup_page.set_content('<h1>Recovery target</h1>')
            await session.switch_tab(crashed_target)

            event = session.event_bus.dispatch(TargetCrashedEvent(
                target_id=crashed_target,
                error='simulated Playwright crash signal',
            ))
            await event
            await event.event_result(raise_if_any=True, raise_if_none=False)
            await asyncio.sleep(0)

            self.assertIs(await session.get_current_page(), backup_page)
            self.assertTrue(crashed_page.is_closed())
            self.assertEqual(errors[-1].error_type, 'TargetCrash')
            self.assertEqual(errors[-1].details['target_id'], crashed_target)
        finally:
            await session.stop()

    async def test_stop_preserves_keep_alive_but_kill_releases_it(self):
        session = CamoufoxSession(
            headless=True,
            humanize=False,
            persistent_context=False,
            keep_alive=True,
        )
        try:
            await session.start()
            original_context = session.session_manager.context
            await session.stop()
            self.assertIs(session.session_manager.context, original_context)
            self.assertTrue(await session.is_active())
            self.assertFalse(session.crash_watchdog.is_monitoring)

            await session.start()
            self.assertIs(session.session_manager.context, original_context)
            self.assertTrue(session.crash_watchdog.is_monitoring)
        finally:
            await session.kill()
        self.assertIsNone(session.session_manager.context)
        self.assertFalse(session.crash_watchdog.is_monitoring)

    async def test_failure_diagnostics_are_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / 'diagnostics'
            session = CamoufoxSession(
                headless=True,
                humanize=False,
                persistent_context=False,
                diagnostics_dir=output,
                diagnostics_max_entries=1,
            )
            try:
                await session.start()
                page = await session.get_current_page()
                await page.set_content(
                    '<input id="password" type="password" value="never-write-this">'
                    '<button id="safe">Continue</button>'
                )

                for number in range(2):
                    event = session.event_bus.dispatch(BrowserErrorEvent(
                        error_type='SyntheticFailure',
                        message=f'diagnostic test {number}',
                        details={
                            'api_token': 'never-write-this',
                            'Authorization': 'Bearer never-write-this',
                            'url': 'https://example.com/private?token=never-write-this',
                        },
                    ))
                    await event
                    await event.event_result(raise_if_any=True, raise_if_none=False)

                bundles = list(output.glob('failure-*'))
                self.assertEqual(len(bundles), 1)
                record = json.loads((bundles[0] / 'event.json').read_text(encoding='utf-8'))
                serialized = json.dumps(record)
                self.assertNotIn('never-write-this', serialized)
                self.assertEqual(record['details']['api_token'], '[REDACTED]')
                self.assertEqual(record['details']['Authorization'], '[REDACTED]')
                self.assertTrue((bundles[0] / 'screenshot.png').read_bytes().startswith(b'\x89PNG'))
                self.assertNotIn(
                    'never-write-this',
                    (bundles[0] / 'dom.txt').read_text(encoding='utf-8'),
                )
                self.assertEqual(len(session.diagnostic_records), 1)
            finally:
                await session.kill()


if __name__ == '__main__':
    unittest.main()

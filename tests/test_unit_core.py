"""Fast regressions for security and relocalization logic that need no browser."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from browser_use.browser.events import (
    BrowserErrorEvent,
    BrowserKillEvent,
    BrowserLaunchEvent,
    BrowserStartEvent,
    BrowserStateRequestEvent,
    BrowserStopEvent,
    ClickCoordinateEvent,
    ClickElementEvent,
    CloseTabEvent,
    GetDropdownOptionsEvent,
    GoBackEvent,
    GoForwardEvent,
    LoadStorageStateEvent,
    NavigateToUrlEvent,
    RefreshEvent,
    SaveStorageStateEvent,
    ScreenshotEvent,
    ScrollEvent,
    ScrollToTextEvent,
    SelectDropdownOptionEvent,
    SendKeysEvent,
    SwitchTabEvent,
    TypeTextEvent,
    UploadFileEvent,
    WaitEvent,
)
from camoufox_browseruse.dom.accessibility import merge_accessibility, parse_aria_snapshot
from camoufox_browseruse.dom.relocalization import find_best_matching_node
from camoufox_browseruse.dom.serializer import node_from_payload, serialize_dom_state
from camoufox_browseruse.session import CamoufoxSession
from camoufox_browseruse.session_manager import CamoufoxSessionManager
from camoufox_browseruse.watchdogs import (
    ActionWatchdog,
    CrashWatchdog,
    DOMWatchdog,
    DownloadsWatchdog,
    ScreenshotWatchdog,
    StorageStateWatchdog,
)
from camoufox_browseruse.watchdogs.diagnostics import _redact, _safe_url


class TestObservationCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_failure_and_cancellation_join_all_observation_tasks(self):
        for fail in (True, False):
            with self.subTest(fail=fail):
                started = set()
                finished = set()
                ready = asyncio.Event()

                async def observe(name):
                    started.add(name)
                    if len(started) == 5:
                        ready.set()
                    try:
                        await ready.wait()
                        if fail and name == 'dom':
                            raise RuntimeError('DOM observation failed')
                        await asyncio.Event().wait()
                    finally:
                        finished.add(name)

                session = SimpleNamespace(
                    get_current_page=AsyncMock(return_value=SimpleNamespace(title=lambda: observe('title'))),
                    take_screenshot=lambda: observe('screenshot'),
                    get_tabs=lambda: observe('tabs'),
                )
                watchdog = DOMWatchdog(session)
                watchdog.get_state = lambda: observe('dom')
                watchdog.page_info = lambda: observe('info')
                task = asyncio.create_task(watchdog.on_BrowserStateRequestEvent(
                    BrowserStateRequestEvent(include_dom=True, include_screenshot=True),
                ))
                await asyncio.wait_for(ready.wait(), timeout=1)
                if not fail:
                    task.cancel()
                with self.assertRaises(RuntimeError if fail else asyncio.CancelledError):
                    await task
                self.assertEqual(finished, {'dom', 'title', 'screenshot', 'tabs', 'info'})


def make_node(index: int, *, text: str = '', attrs: dict[str, str] | None = None, x: int = 0):
    return node_from_payload(
        {
            'tagName': 'BUTTON',
            'text': text,
            'is_visible': True,
            'rect': {'x': x, 'y': 0, 'width': 100, 'height': 30},
        },
        index,
        attrs or {},
    )


class TestRelocalization(unittest.TestCase):
    def test_transient_node_id_does_not_break_tie(self):
        target = make_node(2, text='Save', attrs={'class': 'action'})
        same_index = make_node(2, text='Save', attrs={'class': 'action'}, x=100)
        other_index = make_node(3, text='Save', attrs={'class': 'action'}, x=100)
        self.assertIsNone(find_best_matching_node(target, [same_index, other_index]))

    def test_geometry_alone_is_not_identity(self):
        target = make_node(1)
        candidate = make_node(8)
        self.assertIsNone(find_best_matching_node(target, [candidate]))

    def test_unique_test_id_wins(self):
        target = make_node(1, text='Save', attrs={'data-testid': 'save'})
        expected = make_node(7, text='Save', attrs={'data-testid': 'save'}, x=100)
        rival = make_node(8, text='Save', attrs={'class': 'action'}, x=100)
        self.assertIs(find_best_matching_node(target, [rival, expected]), expected)

    def test_conflicting_id_cannot_relocalize_by_matching_text(self):
        target = make_node(1, text='Save', attrs={'id': 'original'})
        replacement = make_node(1, text='Save', attrs={'id': 'replacement'})
        self.assertIsNone(find_best_matching_node(target, [replacement]))


class TestDOMSerialization(unittest.TestCase):
    def test_static_text_is_visible_but_not_actionable(self):
        rect = {'x': 0, 'y': 0, 'width': 100, 'height': 30}
        state, selector_map = serialize_dom_state(
            [
                {
                    'tagName': 'H1', 'text': 'Static status text',
                    'attributes': {}, 'selector': 'h1', 'ordinal': 0,
                    'is_visible': True, 'is_interactive': False, 'rect': rect,
                },
                {
                    'tagName': 'BUTTON', 'text': 'Continue',
                    'attributes': {'id': 'continue'}, 'selector': '#continue', 'ordinal': 0,
                    'is_visible': True, 'is_interactive': True, 'rect': rect,
                },
            ],
            {},
        )

        self.assertEqual(list(selector_map), [1])
        self.assertEqual(selector_map[1].attributes.get('id'), 'continue')
        representation = state.llm_representation()
        self.assertIn('Static status text', representation)
        self.assertNotIn('<h1', representation)
        self.assertIn('[1]<button', representation)

    def test_selected_ancestors_form_a_real_tree_without_repeated_container_text(self):
        rect = {'x': 0, 'y': 0, 'width': 100, 'height': 30}
        state, selector_map = serialize_dom_state(
            [
                {
                    'tagName': 'ARTICLE', 'text': 'Account Ready Continue',
                    'attributes': {}, 'selector': 'article', 'ordinal': 0,
                    'is_visible': True, 'is_interactive': False, 'rect': rect,
                    'tree_id': 'main:1', 'parent_tree_id': None,
                },
                {
                    'tagName': 'H2', 'text': 'Account Ready',
                    'attributes': {}, 'selector': 'h2', 'ordinal': 0,
                    'is_visible': True, 'is_interactive': False, 'rect': rect,
                    'tree_id': 'main:2', 'parent_tree_id': 'main:1',
                },
                {
                    'tagName': 'BUTTON', 'text': 'Continue',
                    'attributes': {'id': 'continue'}, 'selector': '#continue', 'ordinal': 0,
                    'is_visible': True, 'is_interactive': True, 'rect': rect,
                    'tree_id': 'main:3', 'parent_tree_id': 'main:1',
                },
            ],
            {},
        )

        self.assertEqual(list(selector_map), [1])
        button = selector_map[1]
        self.assertEqual(button.parent_node.node_name, 'ARTICLE')
        self.assertEqual([child.node_name for child in button.parent_node.children_nodes], ['H2', 'BUTTON'])
        representation = state.llm_representation()
        self.assertEqual(representation.count('Account Ready'), 1)
        self.assertEqual(representation.count('Continue'), 1)


class TestAccessibilityEnrichment(unittest.TestCase):
    def test_native_snapshot_is_parsed_and_joined_by_role_and_geometry(self):
        snapshot = '''
        - main "Workspace" [ref=e2] [box=0,0,400,200]:
          - textbox "Search catalog" [ref=e3] [box=20,30,160,25]
          - button "Save" [pressed] [ref=e4] [box=190,30,60,25]
        '''
        elements = [
            {
                'tagName': 'INPUT', 'text': '', 'attributes': {'id': 'q'},
                'rect': {'x': 20, 'y': 30, 'width': 160, 'height': 25},
                'is_interactive': True,
            },
            {
                'tagName': 'BUTTON', 'text': 'Save', 'attributes': {'id': 'save'},
                'rect': {'x': 190, 'y': 30, 'width': 60, 'height': 25},
                'is_interactive': True,
            },
        ]

        parsed = parse_aria_snapshot(snapshot)
        self.assertEqual([node.role for node in parsed], ['main', 'textbox', 'button'])
        self.assertEqual(parsed[0].child_ids, ['aria-2', 'aria-3'])
        merge_accessibility(elements, snapshot)
        self.assertEqual(elements[0]['accessibility']['name'], 'Search catalog')
        self.assertEqual(elements[0]['accessibility']['source'], 'native-aria')
        self.assertIs(elements[1]['accessibility']['properties']['pressed'], True)

    def test_dom_fallback_preserves_semantics_when_native_snapshot_fails(self):
        elements = [{
            'tagName': 'DIV', 'text': 'Dark mode',
            'attributes': {'role': 'switch', 'aria-checked': 'false'},
            'rect': {'x': 0, 'y': 0, 'width': 100, 'height': 30},
            'is_interactive': False, 'is_disabled': False,
        }]
        merge_accessibility(elements, '')
        self.assertEqual(elements[0]['accessibility']['role'], 'switch')
        self.assertIs(elements[0]['accessibility']['properties']['checked'], False)
        self.assertEqual(elements[0]['accessibility']['source'], 'dom-fallback')
        self.assertTrue(elements[0]['is_interactive'])


class TestUrlPolicyParsing(unittest.TestCase):
    def test_exact_host_rejects_suffix_and_userinfo_spoofing(self):
        match = CamoufoxSession._is_url_match
        self.assertTrue(match('https://example.com/path', 'example.com', 'https', 'example.com'))
        self.assertFalse(match('https://example.com.evil.test', 'example.com.evil.test', 'https', 'example.com'))
        self.assertFalse(match('https://example.com@evil.test', 'evil.test', 'https', 'example.com'))

    def test_loopback_aliases_are_ip_policy_targets(self):
        self.assertTrue(CamoufoxSession._is_ip_address('localhost'))
        self.assertTrue(CamoufoxSession._is_ip_address('service.localhost'))
        self.assertTrue(CamoufoxSession._is_ip_address('127.0.0.1'))


class TestEventContract(unittest.TestCase):
    def test_every_browser_use_request_event_has_one_native_owner(self):
        request_events = {
            BrowserStartEvent, BrowserStopEvent, BrowserLaunchEvent, BrowserKillEvent,
            NavigateToUrlEvent, GoBackEvent, GoForwardEvent, RefreshEvent, WaitEvent,
            SwitchTabEvent, CloseTabEvent, ClickElementEvent, ClickCoordinateEvent,
            TypeTextEvent, ScrollEvent, ScrollToTextEvent, SendKeysEvent, UploadFileEvent,
            GetDropdownOptionsEvent, SelectDropdownOptionEvent, BrowserStateRequestEvent,
            ScreenshotEvent, SaveStorageStateEvent, LoadStorageStateEvent,
        }
        owners = (
            CamoufoxSessionManager,
            ActionWatchdog,
            DOMWatchdog,
            DownloadsWatchdog,
            ScreenshotWatchdog,
            StorageStateWatchdog,
        )
        counts = {
            event: sum(event in owner.HANDLERS for owner in owners)
            for event in request_events
        }
        self.assertEqual(
            {event.__name__: count for event, count in counts.items() if count != 1},
            {},
        )


class TestContextContract(unittest.TestCase):
    def test_browser_profile_viewport_mode_reaches_native_context(self):
        headful = CamoufoxSession(headless=False)
        headful_args = headful.session_manager._context_args(
            dict(headful._context_options)
        )
        self.assertIs(headful_args['no_viewport'], True)
        self.assertNotIn('viewport', headful_args)

        headless = CamoufoxSession(headless=True)
        headless_args = headless.session_manager._context_args(
            dict(headless._context_options)
        )
        self.assertIs(headless_args['no_viewport'], False)
        self.assertEqual(
            headless_args['viewport'],
            {'width': 1920, 'height': 1080},
        )


class TestDiagnosticRedaction(unittest.TestCase):
    def test_sensitive_details_and_url_credentials_are_redacted(self):
        self.assertEqual(
            _safe_url('https://user:pass@example.com/path?token=secret#private'),
            'https://example.com/path?[REDACTED]#[REDACTED]',
        )
        self.assertEqual(
            _redact({
                'api_token': 'secret',
                'nested': {'Authorization': 'Bearer private'},
                'url': 'https://example.com/path?token=secret',
            }),
            {
                'api_token': '[REDACTED]',
                'nested': {'Authorization': '[REDACTED]'},
                'url': 'https://example.com/path?[REDACTED]',
            },
        )


class TestCrashHealthDebounce(unittest.IsolatedAsyncioTestCase):
    async def test_requires_control_plane_and_consecutive_failures(self):
        class EventCollector:
            def __init__(self):
                self.events = []

            def dispatch(self, event):
                self.events.append(event)

        class FakeProbe:
            def __init__(self, outcomes):
                self.outcomes = iter(outcomes)

            async def next_outcome(self):
                outcome = next(self.outcomes)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        class FakeContext(FakeProbe):
            async def cookies(self):
                return await self.next_outcome()

        class FakePage(FakeProbe):
            url = 'https://example.test/proof-of-work'

            async def evaluate(self, _expression):
                return await self.next_outcome()

        class FakeSession:
            def __init__(self, context):
                self.event_bus = EventCollector()
                self.agent_focus_target_id = 'target-1'
                self.session_manager = type(
                    'Manager',
                    (),
                    {'context': context},
                )()

        failures = [TimeoutError()] * 5
        context = FakeContext([[], *failures])
        session = FakeSession(context)
        watchdog = CrashWatchdog(session)
        watchdog.health_timeout_seconds = 0.1

        page = FakePage(
            [TimeoutError(), TimeoutError(), 2, TimeoutError(), TimeoutError(), TimeoutError()]
        )
        # A blocked JavaScript main thread is not a browser failure while the
        # Firefox context still answers control-plane commands.
        await watchdog._check_page_health(page, 'target-1')
        self.assertEqual(session.event_bus.events, [])
        self.assertEqual(watchdog._consecutive_health_failures, 0)

        # A single combined page/control failure remains transient.
        await watchdog._check_page_health(page, 'target-1')
        self.assertEqual(session.event_bus.events, [])
        self.assertEqual(watchdog._consecutive_health_failures, 1)

        # A healthy page ping resets the failure sequence.
        await watchdog._check_page_health(page, 'target-1')
        self.assertEqual(watchdog._consecutive_health_failures, 0)
        self.assertEqual(session.event_bus.events, [])

        await watchdog._check_page_health(page, 'target-1')
        await watchdog._check_page_health(page, 'target-1')
        self.assertEqual(len(session.event_bus.events), 1)
        event = session.event_bus.events[0]
        self.assertIsInstance(event, BrowserErrorEvent)
        self.assertEqual(event.error_type, 'BrowserUnresponsive')
        self.assertEqual(
            event.details['error'],
            'page=TimeoutError; control=TimeoutError',
        )
        self.assertEqual(event.details['consecutive_failures'], 2)

        await watchdog._check_page_health(page, 'target-1')
        self.assertEqual(len(session.event_bus.events), 1)


if __name__ == '__main__':
    unittest.main()

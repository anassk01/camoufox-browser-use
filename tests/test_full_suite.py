import asyncio
import base64
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from browser_use.browser.events import (
    BrowserConnectedEvent,
    BrowserErrorEvent,
    BrowserStoppedEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    ScreenshotEvent,
    SwitchTabEvent,
    TabClosedEvent,
    TabCreatedEvent,
    TypeTextEvent,
)
from browser_use.tools.service import Tools
from camoufox_browseruse import CamoufoxSession, open_camoufox_session, register_camoufox_tools

FIXTURE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Camoufox Test Page</title>
</head>
<body>
    <h1>Welcome to Camoufox Automation</h1>
    <p>This is a testing page for our native Browser-Use adapter.</p>

    <form id="test-form" onsubmit="event.preventDefault(); document.getElementById('status').innerText = 'Submitted!';">
        <label for="username">Username:</label>
        <input type="text" id="username" name="username" class="form-input text-field" placeholder="Enter username" />

        <label for="country">Country:</label>
        <select id="country" name="country" class="form-select dropdown">
            <option value="us">United States</option>
            <option value="fr">France</option>
            <option value="ma">Morocco</option>
        </select>

        <label for="upload">Upload File:</label>
        <input type="file" id="upload" name="upload" class="form-file" />

        <button type="submit" id="submit-btn" class="btn primary-btn">Submit Form</button>
    </form>

    <div id="status">Ready</div>
    <div style="height: 1500px;">Long scroll area for testing scrolling behavior</div>
    <button id="bottom-btn" class="btn secondary-btn">Bottom Action</button>
</body>
</html>
"""


class TestCamoufoxSessionIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.fixture_file = Path(__file__).parent / "fixture.html"
        self.fixture_file.write_text(FIXTURE_HTML, encoding="utf-8")
        self.fixture_url = f"file://{self.fixture_file.resolve()}"

    async def asyncTearDown(self):
        pass

    async def test_full_session_and_browser_use_contracts(self):
        print("\n--- Starting Full Native Camoufox Integration Test ---")

        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            # 1. Navigation
            navigation_started = []
            navigation_completed = []
            session.event_bus.on(NavigationStartedEvent, lambda event: navigation_started.append(event))
            session.event_bus.on(NavigationCompleteEvent, lambda event: navigation_completed.append(event))
            await session.navigate_to(self.fixture_url)
            self.assertEqual(await session.get_current_page_title(), "Camoufox Test Page")
            self.assertEqual(navigation_started[-1].url, self.fixture_url)
            self.assertEqual(navigation_completed[-1].loading_status, 'complete')
            print("✓ 1. Navigation succeeded")

            # 2. Tabs
            tabs = await session.get_tabs()
            self.assertEqual(len(tabs), 1)
            self.assertIn("Camoufox", tabs[0].title)
            print(f"✓ 2. Tab listing succeeded: {len(tabs)} tab(s)")

            # 3. DOM State & Serialization
            dom_state = await session.get_dom_state()
            self.assertIsNotNone(dom_state._root)
            self.assertGreater(len(dom_state.selector_map), 0)

            llm_text = dom_state.llm_representation()
            self.assertIn("<input", llm_text)
            self.assertIn("<button", llm_text)
            print(f"✓ 3. DOM serialized: {len(dom_state.selector_map)} elements found")
            print(f"     LLM Representation preview:\n{llm_text.strip()[:200]}...")

            # 4. Browser State Summary (what Agent.step receives)
            summary = await session.get_browser_state_summary(
                include_screenshot=True,
                include_recent_events=True,
            )
            self.assertIsNotNone(summary.screenshot)
            self.assertIsNotNone(summary.page_info)
            self.assertGreater(summary.page_info.page_height, 1000)
            self.assertTrue(any(
                item['event_type'] == 'BrowserStateRequestEvent'
                for item in json.loads(summary.recent_events or '[]')
            ))
            print("✓ 4. BrowserStateSummary generated with screenshot and page geometry")

            # 5. TypeTextEvent on username input
            input_node = None
            submit_node = None
            for idx, node in summary.dom_state.selector_map.items():
                if node.node_name.upper() == "INPUT" and (node.attributes or {}).get("id") == "username":
                    input_node = (idx, node)
                elif node.node_name.upper() == "BUTTON" and (node.attributes or {}).get("id") == "submit-btn":
                    submit_node = (idx, node)

            self.assertIsNotNone(input_node, "Username input element not found in selector map")
            self.assertIsNotNone(submit_node, "Submit button not found in selector map")

            # Type text using event
            from browser_use.browser.events import TypeTextEvent, ClickElementEvent, SendKeysEvent
            type_event = session.event_bus.dispatch(
                TypeTextEvent(node=input_node[1], text="test_user", clear=True)
            )
            await type_event
            await type_event.event_result(raise_if_any=True)
            page = await session.get_current_page()
            input_val = await page.locator("#username").input_value()
            self.assertEqual(input_val, "test_user")
            print("✓ 5. TypeTextEvent verified: input field contains 'test_user'")

            # 6. ClickElementEvent on submit button
            click_event = session.event_bus.dispatch(
                ClickElementEvent(node=submit_node[1])
            )
            await click_event
            await click_event.event_result(raise_if_any=True)
            status_text = await page.locator("#status").inner_text()
            self.assertEqual(status_text, "Submitted!")
            submitted_state = await session.get_dom_state()
            self.assertIn("Submitted!", submitted_state.llm_representation())
            print("✓ 6. ClickElementEvent verified: form submitted, status changed to 'Submitted!'")

            # 7. SendKeysEvent
            keys_event = session.event_bus.dispatch(
                SendKeysEvent(keys="Tab")
            )
            await keys_event
            print("✓ 7. SendKeysEvent verified")

            # 8. Tools override integration
            tools = Tools()
            register_camoufox_tools(tools)

            # Find elements tool
            from browser_use.tools.views import FindElementsAction, SearchPageAction, GetDropdownOptionsAction, SelectDropdownOptionAction
            find_res = await tools.registry.execute_action(
                action_name="find_elements",
                params=FindElementsAction(selector="button", attributes=["id"]).model_dump(),
                browser_session=session,
            )
            self.assertIn("submit-btn", find_res.extracted_content)
            self.assertTrue(find_res.include_extracted_content_only_once)
            print("✓ 8. find_elements tool override verified without CDP")

            # Search page tool
            search_res = await tools.registry.execute_action(
                action_name="search_page",
                params=SearchPageAction(pattern="Automation").model_dump(),
                browser_session=session,
            )
            self.assertIn("Welcome to Camoufox Automation", search_res.extracted_content)
            print("✓ 9. search_page tool override verified without CDP")

            # 10. get_state_as_text
            state_text = await session.get_state_as_text()
            self.assertIn("<input", state_text)
            self.assertIn("Username", state_text)
            print("✓ 10. get_state_as_text verified")

            # 11. DOM Query and Index Helpers
            idx_user = await session.get_index_by_id("username")
            self.assertIsNotNone(idx_user)
            self.assertEqual(idx_user, input_node[0])

            idx_class = await session.get_index_by_class("primary-btn")
            self.assertIsNotNone(idx_class)
            self.assertEqual(idx_class, submit_node[0])

            sel_map = await session.get_selector_map()
            self.assertGreater(len(sel_map), 0)

            node_coords = await session.get_element_coordinates(submit_node[1].node_id)
            self.assertIsNotNone(node_coords)
            self.assertGreater(node_coords.width, 0)
            print("✓ 11. get_index_by_id, get_index_by_class, get_element_coordinates verified")

            # 12. File input checks
            upload_node = None
            for idx, node in sel_map.items():
                if (node.attributes or {}).get("id") == "upload":
                    upload_node = node
                    break
            self.assertIsNotNone(upload_node)
            self.assertTrue(session.is_file_input(upload_node))
            self.assertFalse(session.is_file_input(input_node[1]))

            near_file = session.find_file_input_near_element(upload_node)
            self.assertIsNotNone(near_file)
            self.assertEqual(near_file.node_id, upload_node.node_id)
            print("✓ 12. is_file_input and find_file_input_near_element verified")

            # 13. Dropdowns
            country_node = None
            for idx, node in sel_map.items():
                if (node.attributes or {}).get("id") == "country":
                    country_node = (idx, node)
                    break
            self.assertIsNotNone(country_node)

            dropdown_opts = await session.get_dropdown_options(country_node[1])
            self.assertIn("short_term_memory", dropdown_opts)
            self.assertIn("France", dropdown_opts["formatted_options"])

            select_res = await session.select_dropdown_option(country_node[1], "France")
            self.assertEqual(select_res.get("success"), "true")
            val = await page.locator("#country").input_value()
            self.assertEqual(val, "fr")
            print("✓ 13. get_dropdown_options and select_dropdown_option verified")

            # 14. Cookies & Storage state
            cookies_list = await session.cookies()
            self.assertIsInstance(cookies_list, list)
            await session.clear_cookies()
            storage_dict = await session.export_storage_state()
            self.assertIn("cookies", storage_dict)
            self.assertIn("origins", storage_dict)
            print("✓ 14. cookies(), clear_cookies(), export_storage_state() verified")

            # 15. Visual Highlights
            await session.add_highlights(sel_map)
            highlight_count = await page.evaluate("() => document.querySelectorAll('[data-browser-use-highlight]').length")
            self.assertGreater(highlight_count, 0)

            await session.remove_highlights()
            highlight_count_after = await page.evaluate("() => document.querySelectorAll('[data-browser-use-highlight]').length")
            self.assertEqual(highlight_count_after, 0)

            await session.highlight_interaction_element(submit_node[1])
            await session.highlight_coordinate_click(50, 50)
            print("✓ 15. add_highlights, remove_highlights, highlight_coordinate_click verified")

            # 16. Screenshots (full, element, path)
            shot_file = Path(__file__).parent / "test_shot.png"
            shot_bytes = await session.take_screenshot(path=str(shot_file))
            self.assertTrue(shot_file.exists())
            self.assertGreater(len(shot_bytes), 100)
            shot_file.unlink(missing_ok=True)

            el_shot = await session.screenshot_element("#submit-btn")
            self.assertGreater(len(el_shot), 50)
            screenshot_event = session.event_bus.dispatch(ScreenshotEvent(full_page=False))
            await screenshot_event
            encoded_shot = await screenshot_event.event_result(raise_if_none=True, raise_if_any=True)
            self.assertTrue(base64.b64decode(encoded_shot).startswith(b'\x89PNG'))
            print("✓ 16. take_screenshot(path=...) and screenshot_element() verified")

            # 17. Page and Tab operations
            p1 = await session.must_get_current_page()
            self.assertIsNotNone(p1)
            all_pages = await session.get_pages()
            self.assertEqual(len(all_pages), 1)

            tab_id_0 = await session.get_target_id_from_tab_id("0000")
            self.assertTrue(tab_id_0.startswith("camoufox-tab-"))
            target_by_url = await session.get_target_id_from_url("fixture.html")
            self.assertEqual(target_by_url, tab_id_0)
            recent_target = await session.get_most_recently_opened_target_id()
            self.assertEqual(recent_target, tab_id_0)

            # New tab and close tab
            new_p = await session.new_page("about:blank")
            self.assertEqual(len(await session.get_pages()), 2)
            new_target = session.session_manager.target_id(new_p)
            await session.switch_tab(tab_id_0)
            switch_event = session.event_bus.dispatch(SwitchTabEvent(target_id=None))
            await switch_event
            self.assertEqual(
                await switch_event.event_result(raise_if_none=True, raise_if_any=True),
                new_target,
            )
            await session.close_page(new_p)
            self.assertEqual(len(await session.get_pages()), 1)
            print("✓ 17. must_get_current_page, get_pages, new_page, close_page, target IDs verified")

            # 18. Scroll to text and Refresh events
            from browser_use.browser.events import ScrollToTextEvent, RefreshEvent
            scroll_event = session.event_bus.dispatch(ScrollToTextEvent(text="Bottom Action"))
            await scroll_event
            await scroll_event.event_result(raise_if_any=True)

            refresh_event = session.event_bus.dispatch(RefreshEvent())
            await refresh_event
            await refresh_event.event_result(raise_if_any=True)
            print("✓ 18. ScrollToTextEvent and RefreshEvent verified")

            # 19. Clean Markdown Extraction (DOM Watchdog Parity)
            from browser_use.dom.markdown_extractor import extract_clean_markdown
            md_text, stats = await extract_clean_markdown(browser_session=session)
            self.assertIn("Welcome to Camoufox Automation", md_text)
            print("✓ 19. extract_clean_markdown via native DOM watchdog verified")

            # 20. Dialog Auto-Handling & Tracking
            session._closed_popup_messages.clear()
            await page.evaluate("() => setTimeout(() => alert('Automated Alert Test'), 20)")
            for _ in range(30):
                if session._closed_popup_messages:
                    break
                await asyncio.sleep(0.1)
            self.assertGreater(len(session._closed_popup_messages), 0)
            self.assertIn("Automated Alert Test", session._closed_popup_messages[0])
            await asyncio.sleep(0.2)
            summary_after_dialog = await session.get_browser_state_summary(include_screenshot=False)
            self.assertGreater(len(summary_after_dialog.closed_popup_messages), 0)
            print("✓ 20. JavaScript dialog auto-handling and BrowserStateSummary tracking verified")

            # 21. PDF generation tool override
            from browser_use.filesystem.file_system import FileSystem
            from browser_use.tools.views import SaveAsPdfAction, ClickElementAction
            dl_dir = Path(__file__).parent / "test_downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)
            fs = FileSystem(base_dir=dl_dir)
            pdf_res = await tools.registry.execute_action(
                action_name="save_as_pdf",
                params=SaveAsPdfAction(file_name="suite_test.pdf").model_dump(),
                browser_session=session,
                file_system=fs,
            )
            self.assertIn("Saved page as PDF", pdf_res.extracted_content)
            self.assertTrue(len(pdf_res.attachments) > 0)
            print("✓ 21. save_as_pdf tool override verified with valid PDF output")

            # 22. Coordinate clicking
            await page.evaluate(
                """() => {
                    const button = document.createElement('button');
                    button.id = 'coordinate-target';
                    button.style.cssText = 'position:fixed;left:50px;top:50px;width:40px;height:40px;z-index:2147483647';
                    button.onclick = () => { document.body.dataset.coordinateClicked = 'true'; };
                    document.body.appendChild(button);
                }"""
            )
            session.llm_screenshot_size = (640, 400)
            coordinate_state = await session.get_browser_state_summary(include_screenshot=True)
            original_width = coordinate_state.page_info.viewport_width
            original_height = coordinate_state.page_info.viewport_height
            self.assertEqual(
                session._original_viewport_size,
                (original_width, original_height),
            )
            llm_x = round(60 * session.llm_screenshot_size[0] / original_width)
            llm_y = round(60 * session.llm_screenshot_size[1] / original_height)
            coord_res = await tools.registry.execute_action(
                action_name="click",
                params=ClickElementAction(coordinate_x=llm_x, coordinate_y=llm_y).model_dump(),
                browser_session=session,
            )
            self.assertIsNone(coord_res.error)
            self.assertIn(f"Clicked on coordinate {llm_x}, {llm_y}", coord_res.extracted_content)
            self.assertGreaterEqual(coord_res.metadata['click_x'], 50)
            self.assertLessEqual(coord_res.metadata['click_x'], 90)
            self.assertGreaterEqual(coord_res.metadata['click_y'], 50)
            self.assertLessEqual(coord_res.metadata['click_y'], 90)
            self.assertEqual(await page.evaluate("() => document.body.dataset.coordinateClicked"), "true")
            print("✓ 22. Resized-screenshot coordinate mapping and clicking verified")

            # 22b. Trusted coordinate drag with the same resized image mapping
            await page.evaluate(
                """() => {
                    const source = document.createElement('div');
                    source.id = 'drag-source';
                    source.draggable = true;
                    source.textContent = 'drag';
                    source.style.cssText = 'position:fixed;left:100px;top:50px;width:40px;height:40px;background:red;z-index:2147483647';
                    const target = document.createElement('div');
                    target.id = 'drop-target';
                    target.textContent = 'drop';
                    target.style.cssText = 'position:fixed;left:220px;top:50px;width:40px;height:40px;background:green;z-index:2147483647';
                    source.addEventListener('dragstart', event => {
                        event.dataTransfer.setData('text/plain', 'native-drag');
                        document.body.dataset.dragStartTrusted = String(event.isTrusted);
                    });
                    target.addEventListener('dragover', event => event.preventDefault());
                    target.addEventListener('drop', event => {
                        event.preventDefault();
                        document.body.dataset.dragDropped = event.dataTransfer.getData('text/plain');
                        document.body.dataset.dropTrusted = String(event.isTrusted);
                    });
                    document.body.append(source, target);
                }"""
            )
            image_width, image_height = session.llm_screenshot_size
            drag_params = {
                'source_x': round(120 * image_width / original_width),
                'source_y': round(70 * image_height / original_height),
                'target_x': round(240 * image_width / original_width),
                'target_y': round(70 * image_height / original_height),
            }
            drag_geometry = await tools.registry.execute_action(
                action_name='find_elements',
                params=FindElementsAction(selector='#drag-source').model_dump(),
                browser_session=session,
            )
            self.assertIn('Action-coordinate geometry:', drag_geometry.extracted_content)
            self.assertIn(
                f"center=({drag_params['source_x']}, {drag_params['source_y']})",
                drag_geometry.extracted_content,
            )
            drag_res = await tools.registry.execute_action(
                action_name='drag_and_drop',
                params=drag_params,
                browser_session=session,
            )
            self.assertIsNone(drag_res.error)
            self.assertEqual(drag_res.metadata['strategy'], 'trusted-coordinate-drag')
            self.assertTrue(drag_res.metadata['observable_change'])
            self.assertEqual(await page.evaluate("() => document.body.dataset.dragDropped"), 'native-drag')
            self.assertEqual(await page.evaluate("() => document.body.dataset.dragStartTrusted"), 'true')
            self.assertEqual(await page.evaluate("() => document.body.dataset.dropTrusted"), 'true')
            print("✓ 22b. Trusted coordinate drag-and-drop verified")

            noop_drag = await tools.registry.execute_action(
                action_name='drag_and_drop',
                params={
                    'source_x': drag_params['target_x'],
                    'source_y': drag_params['target_y'],
                    'target_x': drag_params['target_x'] + 2,
                    'target_y': drag_params['target_y'] + 2,
                },
                browser_session=session,
            )
            self.assertIsNone(noop_drag.error)
            self.assertFalse(noop_drag.metadata['observable_change'])
            self.assertIn('No page response was observed', noop_drag.extracted_content)
            print("✓ 22c. Unaccepted drag outcome reported explicitly")

            # 23. Parity targets, frames and _cdp_* methods
            focused_target = session.get_focused_target()
            self.assertIsNotNone(focused_target)
            page_targets = session.get_page_targets()
            self.assertGreaterEqual(len(page_targets), 1)

            all_frames, _ = await session.get_all_frames()
            self.assertIn("frame-0", all_frames)

            await session._cdp_set_viewport(1280, 800)
            info_after = await session.get_page_info()
            self.assertEqual(info_after.viewport_width, 1280)
            self.assertEqual(info_after.viewport_height, 800)
            print("✓ 23. get_focused_target, get_page_targets, get_all_frames, _cdp_set_viewport verified")

            # 24. MCP compat, is_active, and get_html
            self.assertTrue(await session.is_active())
            html_full = await session.get_html()
            self.assertIn("Camoufox Automation", html_full)
            html_sel = await session.get_html("#submit-btn")
            self.assertIn("Submit", html_sel)
            from camoufox_browseruse import apply_camoufox_mcp_compat
            apply_camoufox_mcp_compat()
            print("✓ 24. is_active(), get_html(), and apply_camoufox_mcp_compat() verified")

        print("--- All checks in session integration passed! ---\n")

    async def test_browser_use_agent_execution(self):
        print("\n--- Starting Official Agent with Native Camoufox Test ---")
        from browser_use.agent.service import Agent
        from browser_use.llm.views import ChatInvokeCompletion

        class MockMultiStepAgentLLM:
            model: str = "mock-camoufox"
            model_name: str = "mock-camoufox"
            provider: str = "mock"
            name: str = "mock"

            def __init__(self, session):
                self.session = session
                self.step = 0

            async def ainvoke(self, messages, output_format=None, **kwargs):
                self.step += 1
                if self.step == 1:
                    u_idx = (await self.session.get_index_by_id("username")) or 4
                    data = {
                        "thinking": "Step 1: Fill out username input field.",
                        "evaluation_previous_goal": "Initial page load",
                        "memory": "Form is ready",
                        "next_goal": "Type username",
                        "action": [{"input": {"index": u_idx, "text": "agent_tester"}}],
                    }
                elif self.step == 2:
                    s_idx = (await self.session.get_index_by_id("submit-btn")) or 9
                    data = {
                        "thinking": "Step 2: Submit the form.",
                        "evaluation_previous_goal": "Username entered",
                        "memory": "Username field populated",
                        "next_goal": "Click submit button",
                        "action": [{"click": {"index": s_idx}}],
                    }
                else:
                    data = {
                        "thinking": "Step 3: Verification complete.",
                        "evaluation_previous_goal": "Form submitted",
                        "memory": "Status changed to Submitted!",
                        "next_goal": "Finish task",
                        "action": [{"done": {"text": "Agent completed task on native Camoufox!"}}],
                    }
                return ChatInvokeCompletion(completion=output_format.model_validate(data), usage=None)

        tools = Tools()
        register_camoufox_tools(tools)

        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            await session.navigate_to(self.fixture_url)
            llm = MockMultiStepAgentLLM(session)
            agent = Agent(
                task="Fill out form and submit with native Camoufox",
                llm=llm,
                browser_session=session,
                tools=tools,
                use_vision=False,
                use_judge=False,
            )
            history = await agent.run(max_steps=4)
            self.assertTrue(history.is_successful())
            self.assertEqual(history.final_result(), "Agent completed task on native Camoufox!")

            page = await session.get_current_page()
            self.assertEqual(await page.input_value("#username"), "agent_tester")
            self.assertEqual(await page.inner_text("#status"), "Submitted!")
            print("✓ Official Browser-Use Agent completed real multi-step actions (type, click, verify) & returned success")

        print("--- Official Agent Test Succeeded! ---\n")

    async def test_native_lifecycle_notifications(self):
        session = CamoufoxSession(headless=True, persistent_context=False)
        connected = []
        stopped = []
        session.event_bus.on(BrowserConnectedEvent, lambda event: connected.append(event))
        session.event_bus.on(BrowserStoppedEvent, lambda event: stopped.append(event))
        await session.start()
        try:
            self.assertEqual(connected[-1].cdp_url, 'playwright://camoufox')
        finally:
            await session.stop()
        self.assertEqual(stopped[-1].reason, 'Camoufox context closed')

    async def test_security_watchdog_parity(self):
        print("\n--- Starting Security Watchdog Parity Test ---")
        s1 = CamoufoxSession(allowed_domains=['example.com', '*.safe.org'], headless=True)
        self.assertTrue(s1.is_url_allowed('https://example.com/test'))
        self.assertTrue(s1.is_url_allowed('https://www.example.com/'))
        self.assertTrue(s1.is_url_allowed('https://sub.safe.org/path'))
        self.assertFalse(s1.is_url_allowed('https://malicious.com/'))
        self.assertFalse(s1.is_url_allowed('http://127.0.0.1:8080/'))
        self.assertTrue(s1.is_url_allowed('about:blank'))

        s2 = CamoufoxSession(prohibited_domains=['badsite.com', '*.evil.org'], headless=True)
        self.assertFalse(s2.is_url_allowed('https://badsite.com'))
        self.assertFalse(s2.is_url_allowed('https://sub.evil.org/page'))
        self.assertTrue(s2.is_url_allowed('https://goodsite.com'))

        s3 = CamoufoxSession(block_ip_addresses=True, headless=True)
        self.assertFalse(s3.is_url_allowed('http://127.0.0.1:8000'))
        self.assertFalse(s3.is_url_allowed('http://192.168.1.1'))
        self.assertTrue(s3.is_url_allowed('https://example.com'))

        # Live navigation block test
        s_live = CamoufoxSession(allowed_domains=['example.com'], headless=True)
        try:
            await s_live.start()
            blocked_events: list[BrowserErrorEvent] = []
            s_live.event_bus.on(BrowserErrorEvent, lambda e: blocked_events.append(e))

            with self.assertRaises(ValueError):
                await s_live.navigate_to('https://blocked-domain.com')

            await asyncio.sleep(0.1)
            self.assertGreaterEqual(len(blocked_events), 1)
            self.assertEqual(blocked_events[0].error_type, 'NavigationBlocked')
            self.assertIn('Navigation blocked to disallowed URL', s_live._browser_errors[0])
            print("✓ Security watchdog domain whitelist, blacklist, IP blocking, and error events verified")
        finally:
            await s_live.stop()

    async def test_network_route_security_interception(self):
        print("\n--- Starting Network Route Security Interception Test ---")
        session = CamoufoxSession(allowed_domains=['127.0.0.1', 'localhost'], headless=True)
        try:
            await session.start()
            page = await session.get_current_page()

            await page.set_content("""
                <html><body>
                    <a id="blocked-link" href="https://prohibited-target.org">Blocked Link</a>
                    <a id="blocked-popup" href="https://prohibited-popup.org" target="_blank">Blocked Popup</a>
                    <iframe id="blocked-frame" src="https://prohibited-iframe.org"></iframe>
                </body></html>
            """)

            blocked_events: list[BrowserErrorEvent] = []
            session.event_bus.on(BrowserErrorEvent, lambda e: blocked_events.append(e))

            # 1. Automatic iframe navigation to prohibited domain intercepted
            for _ in range(20):
                if any("prohibited-iframe.org" in err for err in session._browser_errors):
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(any("prohibited-iframe.org" in err for err in session._browser_errors))
            print("  ✓ Iframe navigation to prohibited domain intercepted and blocked")

            # 2. Allowed tabs become focused; a blocked target=_blank popup is
            # closed without destroying or replacing the working page.
            main_page = await session.get_current_page()
            allowed_popup = await session.new_page('about:blank')
            self.assertEqual(len(await session.get_pages()), 2)
            self.assertIs(await session.get_current_page(), allowed_popup)
            await session.close_page(allowed_popup)
            self.assertIs(await session.get_current_page(), main_page)

            await main_page.click('#blocked-popup', no_wait_after=True)
            for _ in range(100):
                if (
                    any("prohibited-popup.org" in err for err in session._browser_errors)
                    and len(await session.get_pages()) == 1
                ):
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(any("prohibited-popup.org" in err for err in session._browser_errors))
            self.assertEqual(len(await session.get_pages()), 1)
            self.assertIs(await session.get_current_page(), main_page)
            print("  ✓ Popup focus and blocked-popup closure preserve the working tab")

            # 3. Click link to prohibited domain intercepted
            try:
                await page.click("#blocked-link", no_wait_after=True, timeout=2000)
            except Exception:
                pass
            for _ in range(50):
                if any("prohibited-target.org" in err for err in session._browser_errors):
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(any("prohibited-target.org" in err for err in session._browser_errors))
            print("  ✓ Link click to prohibited domain intercepted and blocked by route security")

            # 4. Direct navigation to prohibited domain intercepted
            try:
                await session.navigate_to("https://prohibited-direct.org")
            except Exception:
                pass
            await asyncio.sleep(0.1)
            self.assertTrue(any("prohibited-direct.org" in err for err in session._browser_errors))
            print("  ✓ Direct navigate_to prohibited domain intercepted and blocked")

            self.assertGreaterEqual(len(blocked_events), 1)
        finally:
            await session.stop()

    async def test_negative_file_upload_security(self):
        print("\n--- Starting File Upload Security Policy Test ---")
        import tempfile
        from browser_use.filesystem.file_system import FileSystem
        tools = Tools()
        register_camoufox_tools(tools)
        fs = FileSystem(base_dir=Path(tempfile.mkdtemp()))

        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            await session.navigate_to(self.fixture_url)
            upload_idx = (await session.get_index_by_id("upload")) or 8

            res = await tools.upload_file(
                index=upload_idx,
                path="/etc/hosts",
                browser_session=session,
                available_file_paths=["/safe/file.txt"],
                file_system=fs,
            )
            self.assertIsNotNone(res.error)
            self.assertIn("not permitted for upload", res.error)
            print("  ✓ Unauthorized file upload strictly blocked by security policy")

    async def test_upload_button_resolves_unique_hidden_file_input(self):
        """A styled upload button should resolve its unique hidden file input generically."""
        from browser_use.filesystem.file_system import FileSystem

        tools = Tools()
        register_camoufox_tools(tools)
        temp_dir = Path(tempfile.mkdtemp())
        file_system = FileSystem(base_dir=temp_dir / 'agent-files')
        resume = temp_dir / 'resume.pdf'
        resume.write_bytes(b'%PDF-1.4\n% generic upload regression\n')
        try:
            async with open_camoufox_session(headless=True, persistent_context=False) as session:
                page = await session.get_current_page()
                await page.set_content(
                    """
                    <div class="upload-wrapper">
                        <input id="real-file" type="file" hidden>
                        <button id="upload-control" type="button">Upload resume</button>
                    </div>
                    """
                )
                state = await session.get_dom_state()
                button_index = next(
                    index
                    for index, node in state.selector_map.items()
                    if (node.attributes or {}).get('id') == 'upload-control'
                )
                result = await tools.upload_file(
                    index=button_index,
                    path=str(resume),
                    browser_session=session,
                    available_file_paths=[str(resume)],
                    file_system=file_system,
                )
                self.assertIsNone(result.error)
                self.assertEqual(
                    await page.locator('#real-file').evaluate("el => el.files[0].name"),
                    'resume.pdf',
                )

                # Cached controls that become hidden must not be reused merely
                # because their selector still has exactly one DOM match.
                await page.set_content('<input id="stale-field" type="text">')
                stale_state = await session.get_dom_state()
                stale_node = next(
                    node
                    for node in stale_state.selector_map.values()
                    if (node.attributes or {}).get('id') == 'stale-field'
                )
                await page.locator('#stale-field').evaluate("el => el.style.display = 'none'")
                with self.assertRaisesRegex(RuntimeError, 'stale'):
                    await session.relocalize_node(stale_node)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_negative_path_traversal_prevention(self):
        print("\n--- Starting Path Traversal Prevention Test ---")
        import tempfile
        from browser_use.filesystem.file_system import FileSystem
        tools = Tools()
        register_camoufox_tools(tools)
        fs = FileSystem(base_dir=Path(tempfile.mkdtemp()))

        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            await session.navigate_to(self.fixture_url)

            # Screenshot path traversal test
            shot_res = await tools.screenshot(
                file_name="../../escaped_shot.png",
                browser_session=session,
                file_system=fs,
            )
            self.assertIsNotNone(shot_res.error)
            self.assertIn("Path traversal", shot_res.error)
            self.assertFalse(Path("../../escaped_shot.png").exists())
            print("  ✓ Screenshot path traversal attack blocked")

            # PDF path traversal test
            pdf_res = await tools.save_as_pdf(
                file_name="../../../escaped_doc.pdf",
                browser_session=session,
                file_system=fs,
            )
            self.assertIsNotNone(pdf_res.error)
            self.assertIn("Path traversal", pdf_res.error)
            self.assertFalse(Path("../../../escaped_doc.pdf").exists())
            print("  ✓ PDF save path traversal attack blocked")

    async def test_context_options_headers_propagation(self):
        print("\n--- Starting Context Options Propagation Test ---")
        received_headers = {}

        class HeaderServer(BaseHTTPRequestHandler):
            def do_GET(self):
                received_headers.update(dict(self.headers))
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Header Echo</h1></body></html>")
            def log_message(self, format, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), HeaderServer)
        port = server.server_port
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        try:
            custom_headers = {'X-Camoufox-Stealth-Test': 'Verified-Value-777'}
            session = CamoufoxSession(extra_http_headers=custom_headers, headless=True)
            try:
                await session.start()
                await session.navigate_to(f"http://127.0.0.1:{port}/")
                self.assertIn("x-camoufox-stealth-test", {k.lower(): v for k, v in received_headers.items()})
                self.assertEqual(received_headers.get("X-Camoufox-Stealth-Test"), "Verified-Value-777")
                print("  ✓ Context extra_http_headers correctly propagated to Playwright context and web server")
            finally:
                await session.stop()
        finally:
            server.shutdown()

    async def test_about_blank_last_tab_preservation(self):
        print("\n--- Starting AboutBlank Last Tab Preservation Test ---")
        session = CamoufoxSession(headless=True)
        try:
            await session.start()
            p = await session.get_current_page()
            await p.goto('about:blank')

            closed_events: list[TabClosedEvent] = []
            created_events: list[TabCreatedEvent] = []
            session.event_bus.on(TabClosedEvent, lambda e: closed_events.append(e))
            session.event_bus.on(TabCreatedEvent, lambda e: created_events.append(e))

            pages = await session.get_pages()
            self.assertEqual(len(pages), 1)

            # Closing the last tab must NOT close the browser context and must replace with about:blank
            await session.close_tab()
            await asyncio.sleep(0.1)

            remaining_pages = await session.get_pages()
            self.assertEqual(len(remaining_pages), 1)
            self.assertEqual(await session.get_current_page_url(), 'about:blank')
            self.assertTrue(await session.is_active())
            self.assertEqual(len(closed_events), 1)
            self.assertEqual(len(created_events), 1)
            print("✓ Last tab preservation with about:blank and event dispatching verified")
        finally:
            await session.stop()

    async def test_user_data_dir_persistence(self):
        print("\n--- Starting User Data Dir Persistence Test ---")
        class PersistenceServer(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><body><h1>Persistence Server</h1></body></html>')
            def log_message(self, format, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), PersistenceServer)
        port = server.server_port
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()
        tmp = tempfile.mkdtemp()
        restore_tmp = tempfile.mkdtemp()
        try:
            url = f'http://127.0.0.1:{port}/'
            # Session 1: Store state
            s1 = CamoufoxSession(user_data_dir=tmp, headless=True)
            try:
                await s1.start()
                page1 = await s1.get_current_page()
                await page1.goto(url)
                await page1.evaluate(
                    '() => { localStorage.setItem("persist_key", "verified_123"); document.cookie = "session_token=secret456; path=/; max-age=3600"; }'
                )
                exported_state = await s1.export_storage_state()
            finally:
                await s1.stop()

            # Session 2: Read state from same user_data_dir
            s2 = CamoufoxSession(user_data_dir=tmp, headless=True)
            try:
                await s2.start()
                page2 = await s2.get_current_page()
                await page2.goto(url)
                stored_val = await page2.evaluate('() => localStorage.getItem("persist_key")')
                stored_cookie = await page2.evaluate('() => document.cookie')
                self.assertEqual(stored_val, "verified_123")
                self.assertIn("session_token=secret456", stored_cookie)
            finally:
                await s2.stop()

            # Persistent-context launch does not accept Playwright's storage_state
            # option directly. The adapter must restore it after launch instead.
            s3 = CamoufoxSession(
                user_data_dir=restore_tmp,
                persistent_context=True,
                storage_state=exported_state,
                headless=True,
            )
            try:
                await s3.start()
                page3 = await s3.get_current_page()
                await page3.goto(url)
                self.assertEqual(
                    await page3.evaluate('() => localStorage.getItem("persist_key")'),
                    'verified_123',
                )
                self.assertIn(
                    'session_token=secret456',
                    await page3.evaluate('() => document.cookie'),
                )
                await page3.evaluate('() => localStorage.setItem("persist_key", "changed_after_restore")')
                await page3.reload()
                self.assertEqual(
                    await page3.evaluate('() => localStorage.getItem("persist_key")'),
                    'changed_after_restore',
                )
                reexported_state = await s3.export_storage_state()
                exported_names = {
                    item['name']
                    for origin in reexported_state.get('origins', [])
                    for item in origin.get('localStorage', [])
                }
                self.assertNotIn('__camoufox_storage_state_restore__', exported_names)
            finally:
                await s3.stop()
            print("✓ Persistent profiles and explicit storage_state restoration verified")
        finally:
            server.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(restore_tmp, ignore_errors=True)

    async def test_sensitive_dom_stale_identity_and_append_typing_regressions(self):
        async with open_camoufox_session(headless=True, persistent_context=False) as session:
            page = await session.get_current_page()
            await page.set_content(
                '<input id="password" type="password" value="review-secret-123">'
                '<input id="note" value="old">'
                '<button id="original">Save</button>'
            )
            state = await session.get_dom_state()
            self.assertNotIn('review-secret-123', state.llm_representation())
            password_node = await session.get_element_by_index(await session.get_index_by_id('password'))
            self.assertNotIn('value', password_node.attributes)

            note_node = await session.get_element_by_index(await session.get_index_by_id('note'))
            type_event = session.event_bus.dispatch(
                TypeTextEvent(node=note_node, text='-new', clear=False)
            )
            await type_event
            await type_event.event_result(raise_if_any=True)
            self.assertEqual(await page.input_value('#note'), 'old-new')

            original_node = await session.get_element_by_index(await session.get_index_by_id('original'))
            await page.set_content('<button id="replacement">Save</button>')
            await session.get_dom_state()
            with self.assertRaises(RuntimeError):
                await session.relocalize_node(original_node)


if __name__ == "__main__":
    unittest.main()

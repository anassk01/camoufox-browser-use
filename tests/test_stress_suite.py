import asyncio
import math
import time
import unittest
from pathlib import Path
from browser_use.browser.events import ClickElementEvent, SendKeysEvent, TypeTextEvent
from browser_use.tools.service import Tools
from browser_use.tools.views import FindElementsAction, ScrollAction
from camoufox_browseruse import CamoufoxSession, open_camoufox_session, register_camoufox_tools

STRESS_FIXTURE_PATH = (Path(__file__).parent / "stress_fixture.html").resolve().as_uri()

class CamoufoxStressAndParityTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tools = Tools()
        register_camoufox_tools(self.tools)

    async def test_01_rich_form_inputs(self):
        print("\n--- [Stress Test 1] Rich Form & Input Varieties ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)
            page = await session.get_current_page()

            dom_state = await session.get_dom_state()
            sel_map = dom_state.selector_map

            # 1. contenteditable typing
            rich_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "rich-editor":
                    rich_node = (idx, n)
                    break
            self.assertIsNotNone(rich_node, "rich-editor not found")

            # Type into contenteditable
            ev = TypeTextEvent(node=rich_node[1], text=" - Extended by Camoufox", clear=False)
            task = session.event_bus.dispatch(ev)
            await task
            await task.event_result()
            rich_text = await page.locator("#rich-editor").inner_text()
            self.assertIn("Extended by Camoufox", rich_text)
            print("  ✓ Rich text / contenteditable typing verified")

            # 2. Textarea clear and refill
            area_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "multi-area":
                    area_node = (idx, n)
                    break
            self.assertIsNotNone(area_node, "multi-area not found")
            ev_area = TypeTextEvent(node=area_node[1], text="Brand new multi-line\nTextarea content", clear=True)
            t_area = session.event_bus.dispatch(ev_area)
            await t_area
            await t_area.event_result()
            area_val = await page.locator("#multi-area").input_value()
            self.assertEqual(area_val, "Brand new multi-line\nTextarea content")
            print("  ✓ Textarea clear and multi-line typing verified")

            # 3. Checkbox click
            check_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "test-check":
                    check_node = (idx, n)
                    break
            self.assertIsNotNone(check_node, "test-check not found")
            await session.click_action(check_node[0])
            is_checked = await page.locator("#test-check").is_checked()
            self.assertTrue(is_checked)
            print("  ✓ Checkbox toggle click verified")

            # 4. Dropdown with 50 options
            sel_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "stress-select":
                    sel_node = (idx, n)
                    break
            self.assertIsNotNone(sel_node, "stress-select not found")
            click_select = await self.tools.registry.execute_action(
                action_name='click',
                params={'index': sel_node[0]},
                browser_session=session,
            )
            self.assertIn('Option 42', click_select.extracted_content)
            self.assertEqual(await page.locator('#stress-select').input_value(), 'opt_1')
            opts = await session.get_dropdown_options(sel_node[1])
            self.assertIn("Option 42", opts["formatted_options"])
            select_res = await session.select_dropdown_option(sel_node[1], "Option 42")
            self.assertEqual(select_res.get("success"), "true")
            val = await page.locator("#stress-select").input_value()
            self.assertEqual(val, "opt_42")
            print("  ✓ 50-option select dropdown query and selection verified")

            # 5. Form submission
            submit_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "form-submit-btn":
                    submit_node = (idx, n)
                    break
            self.assertIsNotNone(submit_node, "form-submit-btn not found")
            await session.click_action(submit_node[0])
            status = await page.locator("#form-status").inner_text()
            self.assertEqual(status, "Processed!")
            print("  ✓ Form submission completed successfully")

    async def test_02_stale_mutation_recovery(self):
        print("\n--- [Stress Test 2] Dynamic DOM Mutation Recovery ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)
            page = await session.get_current_page()

            # Initial DOM extraction
            dom_state = await session.get_dom_state()
            target_node = None
            shift_node = None
            for idx, n in dom_state.selector_map.items():
                if "unidentified-action-btn" in (n.attributes or {}).get("class", "") or "Target Node" in n.node_value:
                    target_node = (idx, n)
                elif (n.attributes or {}).get("id") == "shift-btn":
                    shift_node = (idx, n)
            self.assertIsNotNone(target_node, "Target node without ID must be found in selector map")
            self.assertIsNotNone(shift_node, "Shift button must be found in selector map")
            old_idx = target_node[0]

            # Replace the original node and inject buttons before its replacement.
            # The cached CSS selector now resolves to zero elements, so recovery is real.
            await session.click_action(shift_node[0])

            self.assertEqual(await page.locator('.unidentified-action-btn').count(), 0)
            self.assertEqual(await page.locator('.relocated-action-btn').count(), 1)

            # Clicking by the old index must re-extract and semantically relocalize.
            click_res = await session.click_action(old_idx)
            status = await page.locator("#action-status").inner_text()
            self.assertEqual(status, "Action Succeeded!")
            self.assertIn("Clicked element", click_res.extracted_content)
            print("  ✓ Stale element relocalization recovered after DOM injection")

    async def test_03_iframe_and_shadow_dom_interaction(self):
        print("\n--- [Stress Test 3] Iframe & Open Shadow DOM Interaction ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)
            page = await session.get_current_page()

            dom_state = await session.get_dom_state()
            sel_map = dom_state.selector_map

            # Find iframe button
            iframe_btn_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "iframe-btn":
                    iframe_btn_node = (idx, n)
                    break
            self.assertIsNotNone(iframe_btn_node, "iframe-btn should be extracted across frames")

            # Click iframe button
            await session.click_action(iframe_btn_node[0])
            frame_status = await page.frame_locator("#inner-frame").locator("#iframe-status").inner_text()
            self.assertEqual(frame_status, "Iframe Clicked!")
            print("  ✓ Anonymous iframe child element interaction verified")

            # Find Shadow DOM button
            shadow_btn_node = None
            for idx, n in sel_map.items():
                if (n.attributes or {}).get("id") == "shadow-btn":
                    shadow_btn_node = (idx, n)
                    break
            self.assertIsNotNone(shadow_btn_node, "shadow-btn should be extracted from open shadow root")

            # Click shadow button
            await session.click_action(shadow_btn_node[0])
            shadow_text = await page.locator("#shadow-host").evaluate("h => h.shadowRoot.getElementById('shadow-btn').innerText")
            self.assertEqual(shadow_text, "Shadow Clicked!")
            print("  ✓ Open shadow root element extraction and click verified")

    async def test_03b_native_accessibility_semantics(self):
        print("\n--- [Stress Test 3b] Native Accessibility Semantics ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<!doctype html><html><body>
                <main aria-label="Account workspace">
                    <label for="query">Search catalog</label>
                    <input id="query" required>
                    <button id="save" aria-pressed="false">Save</button>
                    <div id="theme" role="switch" aria-checked="true">Dark mode</div>
                </main>
            </body></html>''')
            state = await session.get_dom_state()
            nodes = {
                (node.attributes or {}).get('id'): node
                for node in state.selector_map.values()
            }

            self.assertEqual(nodes['query'].ax_node.role, 'textbox')
            self.assertEqual(nodes['query'].ax_node.name, 'Search catalog')
            self.assertEqual(
                (nodes['query'].attributes or {})['data-browser-use-camoufox-ax-source'],
                'native-aria',
            )
            self.assertIn('ax_name=Search catalog', state.llm_representation())
            save_properties = {item.name: item.value for item in nodes['save'].ax_node.properties or []}
            self.assertIs(save_properties['pressed'], False)
            self.assertEqual(nodes['theme'].ax_node.role, 'switch')
            theme_properties = {item.name: item.value for item in nodes['theme'].ax_node.properties or []}
            self.assertIs(theme_properties['checked'], True)
            print("  ✓ Computed roles, names, states, and label relationships merged into DOM nodes")

    async def test_03c_control_help_and_validation_relationships(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<!doctype html><html><body>
                <main>
                    <label id="email-label" for="email">Work email</label>
                    <p id="email-help">Use the address assigned by your company.</p>
                    <input id="email" type="email" required aria-labelledby="email-label"
                           aria-describedby="email-help" aria-errormessage="email-error"
                           aria-invalid="false">
                    <p id="email-error" role="alert">That address is already registered.</p>
                </main>
            </body></html>''')

            state = await session.get_dom_state()
            email = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'email'
            )
            evidence = (email.attributes or {}).get(
                'data-browser-use-camoufox-semantic-evidence', ''
            )
            self.assertIn('label: Work email', evidence)
            self.assertIn('description: Use the address assigned by your company.', evidence)
            self.assertNotIn('error:', evidence)
            self.assertNotIn('validation:', evidence)
            self.assertIn('description: Use the address assigned by your company.', state.llm_representation())
            self.assertIn('That address is already registered.', state.llm_representation())

            await page.locator('#email').evaluate(
                "element => element.setAttribute('aria-invalid', 'true')"
            )
            await page.locator('#email-error').evaluate(
                "element => element.textContent = 'Enter a different company address.'"
            )
            refreshed = await session.get_dom_state()
            refreshed_email = next(
                node for node in refreshed.selector_map.values()
                if (node.attributes or {}).get('id') == 'email'
            )
            refreshed_evidence = (refreshed_email.attributes or {}).get(
                'data-browser-use-camoufox-semantic-evidence', ''
            )
            self.assertIn('error: Enter a different company address.', refreshed_evidence)
            self.assertIn('validation:', refreshed_evidence)
            self.assertNotIn('already registered', refreshed_evidence)

    async def test_03d_selector_ordinal_includes_hidden_matching_controls(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<!doctype html><html><body>
                <div hidden><button class="modal-submit" type="submit">Search</button></div>
                <form onsubmit="event.preventDefault(); document.querySelector('#status').textContent='submitted'">
                    <button type="submit">Login</button>
                </form>
                <p id="status">ready</p>
            </body></html>''')

            state = await session.get_dom_state()
            login = next(
                node for node in state.selector_map.values()
                if node.node_name.lower() == 'button' and node.node_value == 'Login'
            )
            self.assertEqual(
                (login.attributes or {}).get('data-browser-use-camoufox-selector'),
                'button[type="submit"]',
            )
            self.assertEqual(
                (login.attributes or {}).get('data-browser-use-camoufox-ordinal'),
                '1',
            )
            self.assertEqual(await session.dom_watchdog.locator(login).inner_text(), 'Login')
            await session.click_action(login.node_id)
            self.assertEqual(await page.locator('#status').inner_text(), 'submitted')

    async def test_04_dialog_flood_handling(self):
        print("\n--- [Stress Test 4] Chained JavaScript Dialog Flood ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)
            page = await session.get_current_page()

            dom_state = await session.get_dom_state()
            dlg_node = None
            for idx, n in dom_state.selector_map.items():
                if (n.attributes or {}).get("id") == "trigger-dialogs-btn":
                    dlg_node = (idx, n)
                    break
            self.assertIsNotNone(dlg_node)

            # Trigger chained dialogs by clicking the button
            await session.click_action(dlg_node[0])
            await asyncio.sleep(0.5)

            # Check that closed popup messages recorded all 3
            self.assertGreaterEqual(len(session._closed_popup_messages), 3)
            dialog_texts = " ".join(session._closed_popup_messages)
            self.assertIn("Dialog 1", dialog_texts)
            self.assertIn("Dialog 2", dialog_texts)
            self.assertIn("Confirm 3", dialog_texts)

            # Check that browser state summary receives them
            summary = await session.get_browser_state_summary(include_screenshot=False)
            self.assertGreaterEqual(len(summary.closed_popup_messages), 3)
            # Ensure session is still completely responsive
            title = await session.get_current_page_title()
            self.assertEqual(title, "Camoufox Stress & Parity Fixture")
            print("  ✓ 3 rapid chained dialogs intercepted and auto-accepted without freeze")

    async def test_05_multi_tab_churn_stress(self):
        print("\n--- [Stress Test 5] Multi-Tab Churn & Lifetime Stress ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)

            # Open 4 additional tabs
            tabs_created = []
            for i in range(4):
                p = await session.new_page("about:blank")
                await p.set_content(f"<h1>Tab Custom {i+1}</h1>")
                tabs_created.append(p)

            all_tabs = await session.get_tabs()
            self.assertEqual(len(all_tabs), 5)
            tab_ids = [t.target_id for t in all_tabs]
            print(f"  ✓ Created 5 active tabs cleanly with persistent IDs: {tab_ids}")

            # Switch between tabs using stable ID
            await session.switch_tab(tab_ids[2])
            curr_p = await session.get_current_page()
            self.assertEqual(await curr_p.inner_text("h1"), "Tab Custom 2")

            # Close middle tab (tab 1)
            target_to_close = tab_ids[1]
            await session.close_tab(target_to_close)

            # Verify that subsequent tabs retain their exact original IDs
            remaining_tabs = await session.get_tabs()
            self.assertEqual(len(remaining_tabs), 4)
            remaining_ids = [t.target_id for t in remaining_tabs]
            self.assertNotIn(target_to_close, remaining_ids)
            self.assertIn(tab_ids[0], remaining_ids)
            self.assertIn(tab_ids[2], remaining_ids)
            self.assertIn(tab_ids[3], remaining_ids)
            self.assertIn(tab_ids[4], remaining_ids)

            # Switch to tab 4 using its original stable ID
            await session.switch_tab(tab_ids[4])
            curr_p = await session.get_current_page()
            self.assertEqual(await curr_p.inner_text("h1"), "Tab Custom 4")

            # Close tab 3 using its original stable ID
            await session.close_tab(tab_ids[3])
            tabs_after_close2 = await session.get_tabs()
            self.assertEqual(len(tabs_after_close2), 3)
            final_ids = [t.target_id for t in tabs_after_close2]
            self.assertIn(tab_ids[2], final_ids)
            self.assertIn(tab_ids[4], final_ids)

            # Switch back to main tab
            await session.switch_tab(tab_ids[0])
            self.assertEqual(await session.get_current_page_title(), "Camoufox Stress & Parity Fixture")
            print("  ✓ Tab switching, stable identity preservation, and arbitrary teardown verified")

    async def test_05b_target_blank_click_reports_new_tab(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)
            page = await session.get_current_page()
            await page.evaluate(
                """url => {
                    const anchor = document.createElement('a');
                    anchor.id = 'target-blank-link';
                    anchor.href = url;
                    anchor.target = '_blank';
                    anchor.textContent = 'Open child tab';
                    document.body.prepend(anchor);
                }""",
                STRESS_FIXTURE_PATH + '?child=1',
            )
            state = await session.get_dom_state()
            link_index = next(
                index for index, node in state.selector_map.items()
                if (node.attributes or {}).get('id') == 'target-blank-link'
            )

            result = await self.tools.registry.execute_action(
                action_name='click',
                params={'index': link_index},
                browser_session=session,
            )

            self.assertIsNone(result.error)
            self.assertIn('Automatically switched to new tab', result.extracted_content or '')
            self.assertIn('new_target_id', result.metadata or {})
            self.assertEqual(len(await session.get_tabs()), 2)

    async def test_06_large_dom_stress(self):
        print("\n--- [Stress Test 6] High DOM Node Volume (500+ Items) ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)
            page = await session.get_current_page()

            dom_state = await session.get_dom_state()
            bulk_btn_node = None
            for idx, n in dom_state.selector_map.items():
                if (n.attributes or {}).get("id") == "generate-bulk-btn":
                    bulk_btn_node = (idx, n)
                    break
            self.assertIsNotNone(bulk_btn_node)

            # Click generate bulk button to create 500 items (1,000+ DOM nodes)
            await session.click_action(bulk_btn_node[0])

            t0 = time.perf_counter()
            dom_state_after = await session.get_dom_state()
            elapsed = time.perf_counter() - t0

            element_count = len(dom_state_after.selector_map)
            print(f"  ✓ Extracted {element_count} elements in {elapsed:.3f}s")
            self.assertGreater(element_count, 300, "Should extract all available elements without artificial cap")
            self.assertLess(elapsed, 5.0, "DOM extraction must remain fast under load")

            # Verify find_elements tool override works instantly on large DOM
            find_res = await self.tools.registry.execute_action(
                action_name="find_elements",
                params=FindElementsAction(selector=".item-btn", max_results=10).model_dump(),
                browser_session=session,
            )
            self.assertIn("Found 500 elements", find_res.extracted_content)
            print("  ✓ find_elements queried large DOM instantly")

    async def test_06b_resilient_native_scroll_transactions(self):
        print("\n--- [Stress Test 6b] Measured Native Scroll Transactions ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<!doctype html><html><head><style>
                html { scroll-behavior: smooth; }
                body { margin: 0; }
                #panel { width: 320px; height: 160px; overflow: auto; }
                #panel-content { height: 640px; }
                #page-space { height: 2400px; }
            </style></head><body>
                <output id="wheel-events" data-count="0" data-trusted="true" data-targets=""></output>
                <div id="panel" tabindex="0"><div id="panel-content">Scrollable panel</div></div>
                <div id="page-space">Long page</div>
                <script>
                    addEventListener('wheel', event => {
                        const output = document.querySelector('#wheel-events');
                        output.dataset.count = String(Number(output.dataset.count) + 1);
                        output.dataset.trusted = String(
                            output.dataset.trusted === 'true' && event.isTrusted
                        );
                        output.dataset.targets += ` ${event.target.id || event.target.tagName}`;
                    });
                    document.querySelector('#panel').addEventListener('scroll', () => {
                        if (document.querySelector('#lazy-action')) return;
                        setTimeout(() => {
                            document.querySelector('#panel-content').style.height = '960px';
                            const button = document.createElement('button');
                            button.id = 'lazy-action';
                            button.textContent = 'Loaded after scroll';
                            document.querySelector('#panel-content').appendChild(button);
                        }, 60);
                    });
                </script>
            </body></html>''')

            viewport_height = await page.evaluate('window.innerHeight')
            page_result = await self.tools.registry.execute_action(
                action_name="scroll",
                params=ScrollAction(down=True, pages=0.5).model_dump(),
                browser_session=session,
            )
            page_y = await page.evaluate('window.scrollY')
            self.assertIsNone(page_result.error)
            self.assertAlmostEqual(page_y, round(viewport_height * 0.5), delta=2)
            self.assertEqual(page_result.metadata['moved_pixels'], page_y)
            self.assertFalse(page_result.metadata['at_boundary'])
            self.assertEqual(page_result.metadata['delivery'], 'wheel')
            page_wheel_events = await page.locator('#wheel-events').evaluate('el => ({...el.dataset})')
            self.assertGreater(int(page_wheel_events['count']), 0)
            self.assertEqual(page_wheel_events['trusted'], 'true')

            bottom_result = await self.tools.registry.execute_action(
                action_name="scroll",
                params=ScrollAction(down=True, pages=10).model_dump(),
                browser_session=session,
            )
            page_position = await page.evaluate(
                """() => ({
                    y: scrollY,
                    max: document.scrollingElement.scrollHeight
                        - document.scrollingElement.clientHeight
                })"""
            )
            self.assertEqual(page_position['y'], page_position['max'])
            self.assertTrue(bottom_result.metadata['at_boundary'])
            self.assertIn('Reached the bottom boundary', bottom_result.extracted_content)

            no_progress = await self.tools.registry.execute_action(
                action_name="scroll",
                params=ScrollAction(down=True, pages=0.5).model_dump(),
                browser_session=session,
            )
            self.assertEqual(no_progress.metadata['moved_pixels'], 0)
            self.assertTrue(no_progress.metadata['no_progress'])
            self.assertIn('already at the bottom boundary', no_progress.extracted_content)

            await page.evaluate("window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
            wheel_count_before_panel = int(
                await page.locator('#wheel-events').get_attribute('data-count') or '0'
            )
            state = await session.get_dom_state()
            panel_index = next(
                index for index, node in state.selector_map.items()
                if (node.attributes or {}).get('id') == 'panel'
            )
            panel_to_bottom = await self.tools.registry.execute_action(
                action_name="scroll",
                params=ScrollAction(down=True, pages=10, index=panel_index).model_dump(),
                browser_session=session,
            )
            self.assertAlmostEqual(await page.locator('#panel').evaluate('el => el.scrollTop'), 800, delta=2)
            self.assertEqual(await page.evaluate('window.scrollY'), 0)
            self.assertEqual(panel_to_bottom.metadata['target'], 'element')
            self.assertGreaterEqual(panel_to_bottom.metadata['maximum'], 799)
            self.assertTrue(panel_to_bottom.metadata['at_boundary'])
            self.assertEqual(panel_to_bottom.metadata['delivery'], 'dom-target')
            self.assertIsNone(session.dom_watchdog.current_dom_state)

            refreshed = await session.get_dom_state()
            self.assertTrue(any(
                (node.attributes or {}).get('id') == 'lazy-action'
                for node in refreshed.selector_map.values()
            ))
            await self.tools.registry.execute_action(
                action_name="scroll",
                params=ScrollAction(down=False, pages=10, index=panel_index).model_dump(),
                browser_session=session,
            )
            panel_result = await self.tools.registry.execute_action(
                action_name="scroll",
                params=ScrollAction(down=True, pages=0.5, index=panel_index).model_dump(),
                browser_session=session,
            )
            self.assertAlmostEqual(await page.locator('#panel').evaluate('el => el.scrollTop'), 80, delta=2)
            self.assertEqual(panel_result.metadata['moved_pixels'], 80)
            self.assertEqual(panel_result.metadata['delivery'], 'dom-target')
            wheel_events = await page.locator('#wheel-events').evaluate('el => ({...el.dataset})')
            self.assertEqual(wheel_events['trusted'], 'true')
            self.assertEqual(int(wheel_events['count']), wheel_count_before_panel)
            print("  ✓ Trusted page wheel, isolated nested scrolling, boundaries, and lazy refresh verified")

    async def test_07_rapid_consecutive_actions(self):
        print("\n--- [Stress Test 7] Rapid Consecutive Action Burst ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            await session.navigate_to(STRESS_FIXTURE_PATH)

            t0 = time.perf_counter()
            # Perform 20 actions in rapid succession
            for i in range(10):
                await self.tools.registry.execute_action(
                    action_name="scroll",
                    params=ScrollAction(down=(i % 2 == 0), pages=0.5).model_dump(),
                    browser_session=session,
                )
                await self.tools.registry.execute_action(
                    action_name="evaluate",
                    params={"code": f"window.__burst_counter = {i};"},
                    browser_session=session,
                )

            burst_time = time.perf_counter() - t0
            print(f"  ✓ Completed 20 rapid actions in {burst_time:.2f}s without event bus stalls")
            page = await session.get_current_page()
            counter = await page.evaluate("() => window.__burst_counter")
            self.assertEqual(counter, 9)

    async def test_08_network_error_recovery(self):
        print("\n--- [Stress Test 8] Network Failure & Recovery ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            # Navigate to closed/unreachable local port
            with self.assertRaises(RuntimeError):
                await session.navigate_to("http://127.0.0.1:59999", timeout_ms=3000)

            # Session must remain fully healthy and able to navigate to fixture
            await session.navigate_to(STRESS_FIXTURE_PATH)
            title = await session.get_current_page_title()
            self.assertEqual(title, "Camoufox Stress & Parity Fixture")
            print("  ✓ Handled navigation failure gracefully and recovered session immediately")

    async def test_09_dynamic_interaction_transactions(self):
        print("\n--- [Stress Test 9] Dynamic Interaction Transactions ---")
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content("""<!doctype html><html><body>
                <input class="dynamic-field" aria-label="Dynamic entry" value="old"
                    onfocus="if (!this.dataset.replaced) {
                        this.dataset.replaced = 'yes';
                        const replacement = this.cloneNode(true);
                        this.replaceWith(replacement);
                        replacement.focus();
                    }"
                    oninput="document.querySelector('#typed-status').textContent=this.value">
                <span id="typed-status">old</span>
                <button id="delayed-button"
                    onclick="document.querySelector('#click-status').textContent='clicked'">Delayed click</button>
                <span id="click-status">ready</span>
                <button id="blocked-button"
                    onclick="document.querySelector('#blocked-status').textContent='unsafe-click'">Blocked click</button>
                <span id="blocked-status">safe</span>
                <input id="verified-toggle" type="checkbox">
                <div role="combobox" tabindex="0" aria-expanded="false"
                    onclick="document.querySelector('[role=listbox]').hidden=false; this.setAttribute('aria-expanded','true')">Round trip</div>
                <ul role="listbox" hidden>
                    <li role="option" class="selected" aria-selected="true">Round trip</li>
                    <li role="option" class="choice" aria-selected="false"
                        onclick="const combo=document.querySelector('[role=combobox]');
                            document.querySelector('[role=listbox]').hidden=true;
                            combo.textContent=this.textContent; combo.setAttribute('aria-expanded','false');
                            document.querySelector('[role=option][aria-selected=true]').setAttribute('aria-selected','false');
                            this.setAttribute('aria-selected','true'); this.className='selected'">One way</li>
                </ul>
                <script>addEventListener('scroll', () => document.querySelector('[role=listbox]').hidden=true)</script>
            </body></html>""")

            state = await session.get_dom_state()
            dynamic = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('aria-label') == 'Dynamic entry'
            )
            type_event = TypeTextEvent(node=dynamic, text='replacement-safe', clear=True)
            type_task = session.event_bus.dispatch(type_event)
            await type_task
            type_result = await type_task.event_result(raise_if_any=True)
            self.assertEqual(await page.locator('.dynamic-field').input_value(), 'replacement-safe')
            self.assertEqual(await page.locator('#typed-status').inner_text(), 'replacement-safe')
            self.assertGreaterEqual(type_result['attempts'], 1)

            state = await session.get_dom_state()
            delayed = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'delayed-button'
            )
            # The target was visible when observed. A temporary overlay then
            # appears before the action and must be waited out, not skipped
            # with an untrusted JavaScript click.
            await page.evaluate("""() => {
                const button = document.querySelector('#delayed-button');
                const rect = button.getBoundingClientRect();
                const cover = document.createElement('div');
                cover.id = 'temporary-cover';
                Object.assign(cover.style, {
                    position: 'fixed', left: `${rect.left}px`, top: `${rect.top}px`,
                    width: `${rect.width}px`, height: `${rect.height}px`, zIndex: 9999,
                });
                document.body.appendChild(cover);
                setTimeout(() => cover.remove(), 500);
            }""")
            click_task = session.event_bus.dispatch(ClickElementEvent(node=delayed))
            await click_task
            click_result = await click_task.event_result(raise_if_any=True)
            self.assertEqual(await page.locator('#click-status').inner_text(), 'clicked')
            self.assertEqual(click_result['strategy'], 'trusted-pointer-click')

            state = await session.get_dom_state()
            toggle = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'verified-toggle'
            )
            toggle_task = session.event_bus.dispatch(ClickElementEvent(node=toggle))
            await toggle_task
            await toggle_task.event_result(raise_if_any=True)
            self.assertTrue(await page.locator('#verified-toggle').is_checked())

            state = await session.get_dom_state()
            combo = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('role') == 'combobox'
            )
            combo_task = session.event_bus.dispatch(ClickElementEvent(node=combo))
            await combo_task
            await combo_task.event_result(raise_if_any=True)
            state = await session.get_dom_state()
            one_way = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('role') == 'option'
                and (node.node_value or '').strip() == 'One way'
            )
            self.assertEqual(
                (one_way.attributes or {}).get('data-browser-use-camoufox-selector'),
                '[role="option"]',
            )
            self.assertEqual((await session.dom_watchdog.locator(one_way).text_content() or '').strip(), 'One way')
            option_task = session.event_bus.dispatch(ClickElementEvent(node=one_way))
            await option_task
            await option_task.event_result(raise_if_any=True)
            self.assertEqual((await page.locator('[role=combobox]').text_content() or '').strip(), 'One way')
            self.assertEqual(await page.evaluate('scrollY'), 0)

            state = await session.get_dom_state()
            blocked = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'blocked-button'
            )
            await page.evaluate("""() => {
                const button = document.querySelector('#blocked-button');
                const rect = button.getBoundingClientRect();
                const cover = document.createElement('div');
                Object.assign(cover.style, {
                    position: 'fixed', left: `${rect.left}px`, top: `${rect.top}px`,
                    width: `${rect.width}px`, height: `${rect.height}px`, zIndex: 9999,
                });
                document.body.appendChild(cover);
            }""")
            original_attempts = session.action_watchdog.interactions.max_attempts
            session.action_watchdog.interactions.max_attempts = 1
            try:
                blocked_task = session.event_bus.dispatch(ClickElementEvent(node=blocked))
                await blocked_task
                with self.assertRaises(RuntimeError):
                    await blocked_task.event_result(raise_if_any=True)
            finally:
                session.action_watchdog.interactions.max_attempts = original_attempts
            self.assertEqual(await page.locator('#blocked-status').inner_text(), 'safe')
            print("  ✓ Replaced inputs, overlays, ARIA popovers, trusted clicks, toggle verification, and fail-closed safety passed")

    async def test_09b_atomic_native_input_boundary(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<!doctype html><html><body>
                <input id="serialized">
                <input id="settling" oninput="
                    if (this.value === 'settled') {
                        this.value = 'settl';
                        setTimeout(() => { this.value = 'settled' }, 250);
                    }">
                <input id="partial" data-events="0"
                    oninput="this.dataset.events=String(Number(this.dataset.events)+1); this.value=this.value.slice(0, 1)">
                <div id="async-toggle" role="checkbox" aria-checked="false"
                    aria-labelledby="async-label" data-activations="0"
                    onclick="
                        this.dataset.activations=String(Number(this.dataset.activations)+1);
                        this.classList.add('loading');
                        document.querySelector('#async-label').textContent='Checking';
                        setTimeout(() => {
                            this.classList.remove('loading');
                            this.setAttribute('aria-checked', 'true');
                            document.querySelector('#async-label').textContent='Verified';
                        }, 250)">□</div>
                <span id="async-label">Verify</span>
                <button id="uncertain" role="switch" aria-checked="false" data-activations="0"
                    onclick="this.dataset.activations=String(Number(this.dataset.activations)+1)">One-shot action</button>
            </body></html>''')

            state = await session.get_dom_state()
            serialized = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'serialized'
            )
            original_click = session.action_watchdog.interactions.input.click

            async def delayed_click(locator, *, button):
                await asyncio.sleep(0.2)
                return await original_click(locator, button=button)

            session.action_watchdog.interactions.input.click = delayed_click
            click_task = session.event_bus.dispatch(ClickElementEvent(node=serialized))
            await asyncio.sleep(0.02)
            keys_task = session.event_bus.dispatch(SendKeysEvent(keys='serialized input'))
            await asyncio.gather(click_task, keys_task)
            await click_task.event_result(raise_if_any=True)
            await keys_task.event_result(raise_if_any=True)
            self.assertEqual(await page.locator('#serialized').input_value(), 'serialized input')

            state = await session.get_dom_state()
            settling = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'settling'
            )
            settle_task = session.event_bus.dispatch(
                TypeTextEvent(node=settling, text='settled', clear=True)
            )
            await settle_task
            await settle_task.event_result(raise_if_any=True)
            self.assertEqual(
                await page.locator('#settling').input_value(),
                'settled',
            )

            state = await session.get_dom_state()
            async_toggle = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'async-toggle'
            )
            async_click = session.event_bus.dispatch(
                ClickElementEvent(node=async_toggle)
            )
            await async_click
            await async_click.event_result(raise_if_any=True)
            await page.locator(
                '#async-toggle[aria-checked="true"]'
            ).wait_for(state='attached', timeout=2000)
            self.assertEqual(
                await page.locator('#async-toggle').get_attribute(
                    'data-activations'
                ),
                '1',
            )

            state = await session.get_dom_state()
            uncertain = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'uncertain'
            )
            click_task = session.event_bus.dispatch(ClickElementEvent(node=uncertain))
            await click_task
            with self.assertRaisesRegex(RuntimeError, 'sent at most once'):
                await click_task.event_result(raise_if_any=True)
            self.assertEqual(await page.locator('#uncertain').get_attribute('data-activations'), '1')

            state = await session.get_dom_state()
            partial = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'partial'
            )
            type_task = session.event_bus.dispatch(
                TypeTextEvent(node=partial, text='abcd', clear=False)
            )
            await type_task
            with self.assertRaisesRegex(RuntimeError, 'sent at most once'):
                await type_task.event_result(raise_if_any=True)
            self.assertEqual(await page.locator('#partial').get_attribute('data-events'), '4')

        async with open_camoufox_session(headless=True, humanize=True) as session:
            page = await session.get_current_page()
            await page.set_content('''<!doctype html><html><body>
                <input id="cadence" autofocus data-samples="[]">
                <button id="pointer-target">Pointer target</button>
                <output id="mouse-samples" data-samples="[]" data-trusted="true"></output>
                <script>
                    document.querySelector('#cadence').addEventListener('keydown', event => {
                        const input = event.currentTarget;
                        const samples = JSON.parse(input.dataset.samples);
                        samples.push({time: performance.now(), trusted: event.isTrusted});
                        input.dataset.samples = JSON.stringify(samples);
                    });
                    addEventListener('mousemove', event => {
                        const output = document.querySelector('#mouse-samples');
                        const samples = JSON.parse(output.dataset.samples);
                        samples.push({x: event.clientX, y: event.clientY});
                        output.dataset.samples = JSON.stringify(samples);
                        output.dataset.trusted = String(
                            output.dataset.trusted === 'true' && event.isTrusted
                        );
                    });
                </script>
            </body></html>''')
            self.assertNotIn('humanize', session._launch_options)
            self.assertTrue(session._humanize_interactions_native)
            await page.locator('#cadence').focus()
            keys_task = session.event_bus.dispatch(SendKeysEvent(keys='human cadence'))
            await keys_task
            result = await keys_task.event_result(raise_if_any=True)
            samples = await page.locator('#cadence').evaluate('el => JSON.parse(el.dataset.samples)')
            intervals = [
                samples[index]['time'] - samples[index - 1]['time']
                for index in range(1, len(samples))
            ]
            self.assertEqual(result['strategy'], 'humanized-keyboard')
            self.assertEqual(len(samples), len('human cadence'))
            self.assertTrue(all(sample['trusted'] for sample in samples))
            self.assertGreater(sum(intervals), 400)
            self.assertGreater(max(intervals) - min(intervals), 15)

            state = await session.get_dom_state()
            pointer_target = next(
                node for node in state.selector_map.values()
                if (node.attributes or {}).get('id') == 'pointer-target'
            )
            # Seed an outward curve from the top-left pointer origin. The
            # transport must turn it inward instead of letting the viewport
            # edge flatten the trusted movement path.
            session.action_watchdog.interactions.input._random.seed(2)
            click_task = session.event_bus.dispatch(ClickElementEvent(node=pointer_target))
            await click_task
            click_result = await click_task.event_result(raise_if_any=True)
            pointer_samples = await page.locator('#mouse-samples').evaluate(
                'el => JSON.parse(el.dataset.samples)'
            )
            self.assertEqual(click_result['strategy'], 'trusted-pointer-click')
            self.assertEqual(
                await page.locator('#mouse-samples').get_attribute('data-trusted'),
                'true',
            )
            self.assertGreaterEqual(len(pointer_samples), 5)
            first, last = pointer_samples[0], pointer_samples[-1]
            line_length = math.hypot(last['x'] - first['x'], last['y'] - first['y'])
            deviations = [
                abs(
                    (last['y'] - first['y']) * sample['x']
                    - (last['x'] - first['x']) * sample['y']
                    + last['x'] * first['y']
                    - last['y'] * first['x']
                ) / max(line_length, 1)
                for sample in pointer_samples[1:-1]
            ]
            self.assertGreater(max(deviations), 1)
            print("  ✓ Serialized one-shot input plus trusted variable keyboard and pointer paths passed")

if __name__ == "__main__":
    unittest.main()

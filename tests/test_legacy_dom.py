"""Legacy/custom DOM names must not hide an otherwise editable portal form."""

import unittest
from browser_use import Tools
from camoufox_browseruse import open_camoufox_session, register_camoufox_tools


class LegacyDOMTests(unittest.IsolatedAsyncioTestCase):
    async def test_colon_qualified_tags_preserve_form_observation_and_interaction(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.route('**/*', lambda route: route.abort())
            await page.set_content('''<form>
                <as:ajaxinclude style="display:block">Application details</as:ajaxinclude>
                <x:field name="custom" tabindex="0" style="display:block">Custom field</x:field>
                <v:button class="legacy" tabindex="0" style="display:block">Attach document</v:button>
                <z:control type="text" tabindex="0" style="display:block">Legacy control</z:control>
                <label for="email">Email</label><input id="email" type="email" required>
                <button>Continue</button>
                </form>''')
            state = await session.get_dom_state()
            self.assertGreater(len(state.selector_map), 0)
            self.assertIn('Application details', state.llm_representation())
            for node in state.selector_map.values():
                if ':' in node.node_name:
                    self.assertEqual(await session.locator_for_node(node).count(), 1)
            index = await session.get_index_by_id('email')
            self.assertIsNotNone(index)
            tools = Tools()
            register_camoufox_tools(tools)
            result = await tools.input(index=index, text='fixture@example.com', browser_session=session)
            self.assertIsNone(result.error)
            self.assertEqual(await page.locator('#email').input_value(), 'fixture@example.com')

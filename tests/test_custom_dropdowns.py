"""Real Firefox dropdown selection across native and custom form widgets."""

import unittest

from browser_use import Tools
from camoufox_browseruse import open_camoufox_session, register_camoufox_tools


class CustomDropdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_ids_and_reordering_do_not_redirect_an_answer(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<div id="questionnaire"><div id="questionnaire"></div>
                <button id="questionnaire" name="country" aria-haspopup="listbox" aria-expanded="false"
                    aria-controls="countries" onclick="this.setAttribute('aria-expanded','true');document.querySelector('#countries').hidden=false">Select One</button>
                <button id="questionnaire" name="authorization" aria-haspopup="listbox" aria-expanded="false"
                    aria-controls="authorization-options" onclick="this.setAttribute('aria-expanded','true');document.querySelector('#authorization-options').hidden=false">Select One</button></div>
                <ul id="countries" role="listbox" hidden><li role="option">Yes</li></ul>
                <ul id="authorization-options" role="listbox" hidden><li role="option" onclick="
                    document.querySelector('[name=authorization]').textContent='Yes';
                    document.querySelector('[name=authorization]').setAttribute('aria-expanded','false');
                    this.parentElement.hidden=true">Yes</li></ul>''')
            tools = Tools()
            register_camoufox_tools(tools)
            state = await session.get_dom_state()
            node = next(node for node in state.selector_map.values()
                        if (node.attributes or {}).get('name') == 'authorization')
            # Reordering identical-looking controls invalidates the old ordinal.
            # Recovery must use the captured field name, not the duplicated id.
            await page.locator('[name=authorization]').evaluate('el => el.parentElement.prepend(el)')
            result = await session.select_dropdown_option(node, 'Yes')
            self.assertEqual(result.get('success'), 'true')
            self.assertEqual(await page.locator('[name=authorization]').inner_text(), 'Yes')
            self.assertEqual(await page.locator('[name=country]').inner_text(), 'Select One')
            self.assertFalse(await page.locator('#countries').is_visible())

    async def test_owned_clipped_dropdown_commits_once_even_when_focus_moved(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<body data-clicks="0" data-submitted="0">
                <button id="choice" aria-haspopup="listbox" aria-expanded="false"
                    onclick="this.setAttribute('aria-expanded','true');this.setAttribute('aria-controls','choices');document.querySelector('#choices').hidden=false">Select One</button>
                <input id="surname" value="Surname">
                <ul role="listbox" id="unrelated"><li role="option">Target choice</li></ul>
                <ul role="listbox" id="choices" hidden style="height:75px;overflow:auto;padding:0">
                    <li role="option" aria-disabled="true" style="height:30px">Select One</li>
                    <li role="option" style="height:30px">First choice</li>
                    <li role="option" style="height:30px">Second choice</li>
                    <li role="option" style="height:30px">Third choice</li>
                    <li role="option" id="target" style="height:30px" onclick="
                        document.body.dataset.clicks=Number(document.body.dataset.clicks)+1;
                        document.querySelector('#choice').textContent=this.textContent;
                        document.querySelector('#choice').setAttribute('aria-expanded','false');
                        document.querySelector('#choices').hidden=true">Target choice</li>
                </ul>
                <button onclick="document.body.dataset.submitted='1'">Submit Application</button>
                </body>''')
            tools = Tools()
            register_camoufox_tools(tools)
            await session.get_dom_state()
            index = await session.get_index_by_id('choice')
            options = await tools.registry.execute_action('dropdown_options', {'index': index}, browser_session=session)
            self.assertIsNone(options.error)
            self.assertIn('Target choice', options.extracted_content)
            # The old workflow sent Enter to this field instead of committing the popup.
            await page.locator('#surname').focus()
            await session.get_dom_state()
            selected = await tools.registry.execute_action('select_dropdown', {
                'index': await session.get_index_by_id('choice'), 'text': 'Target choice'}, browser_session=session)
            self.assertIsNone(selected.error)
            self.assertEqual(await page.locator('#choice').inner_text(), 'Target choice')
            self.assertEqual(await page.locator('body').get_attribute('data-clicks'), '1')
            self.assertEqual(await page.locator('body').get_attribute('data-submitted'), '0')
            self.assertEqual(await page.locator('#surname').input_value(), 'Surname')

    async def test_missing_duplicate_disabled_and_uncommitted_choices_fail_closed(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            tools = Tools()
            register_camoufox_tools(tools)
            for label, choices in (
                ('Missing', '<li role="option">Different</li>'),
                ('Duplicate', '<li role="option">Duplicate</li><li role="option">Duplicate</li>'),
                ('Disabled', '<li role="option" aria-disabled="true">Disabled</li>'),
                ('Ignored', '<li role="option" onclick="document.body.dataset.clicks=\'1\'">Ignored</li>'),
            ):
                with self.subTest(label=label):
                    await page.set_content(f'''<body data-clicks="0"><button id="choice" aria-haspopup="listbox"
                        aria-expanded="true" aria-controls="choices">Select One</button>
                        <ul role="listbox" id="choices">{choices}</ul></body>''')
                    await session.get_dom_state()
                    result = await tools.registry.execute_action('select_dropdown', {
                        'index': await session.get_index_by_id('choice'), 'text': label}, browser_session=session)
                    self.assertIsNotNone(result.error)
                    expected_clicks = '1' if label == 'Ignored' else '0'
                    self.assertEqual(await page.locator('body').get_attribute('data-clicks'), expected_clicks)

    async def test_native_select_and_iframe_combobox_keep_their_scope(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.set_content('''<select id="native"><option>First</option><option>Second</option></select>
                <ul role="listbox"><li role="option">Yes</li></ul>
                <iframe id="child"></iframe>''')
            frame = page.frames[1]
            await frame.set_content('''<div id="combo" role="combobox" tabindex="0" aria-expanded="true"
                aria-controls="answers">Select One</div><ul role="listbox" id="answers">
                <li role="option" onclick="document.querySelector('#combo').textContent='Yes';
                document.querySelector('#combo').setAttribute('aria-expanded','false');this.parentElement.hidden=true">Yes</li></ul>''')
            tools = Tools()
            register_camoufox_tools(tools)
            await session.get_dom_state()
            for control, answer in (('native', 'Second'), ('combo', 'Yes')):
                result = await tools.registry.execute_action('select_dropdown', {
                    'index': await session.get_index_by_id(control), 'text': answer}, browser_session=session)
                self.assertIsNone(result.error)
                await session.get_dom_state()
            self.assertEqual(await page.locator('#native').input_value(), 'Second')
            self.assertEqual(await frame.locator('#combo').inner_text(), 'Yes')

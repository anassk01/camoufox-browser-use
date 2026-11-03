"""Native interaction with ARIA dropdowns, scoped to their owning control."""

from __future__ import annotations

import asyncio
import json

from browser_use.dom.views import EnhancedDOMTreeNode


OPTION_SELECTOR = '[role="option"], [role="menuitem"], [role="menuitemradio"]'


async def custom_dropdown(session, node: EnhancedDOMTreeNode):
    """Open one declared dropdown and resolve its owned popup in the same frame."""
    node = await session.dom_watchdog.relocalize(node)
    control = session.dom_watchdog.locator(node)
    attrs = await control.evaluate('el => ({role: el.getAttribute("role"), popup: el.getAttribute("aria-haspopup"), expanded: el.getAttribute("aria-expanded")})')
    if attrs['role'] != 'combobox' and attrs['popup'] not in {'listbox', 'menu', 'true'}:
        raise ValueError('Target is not a native select or an ARIA dropdown control.')
    frame_id = (node.attributes or {}).get('data-browser-use-camoufox-frame', 'main')
    frame = session.dom_watchdog.frame_registry.frames_by_id[frame_id]
    if attrs['expanded'] != 'true':
        await session.action_watchdog.interactions.click(node)

    deadline = asyncio.get_running_loop().time() + 2.5
    while True:
        owner_ids = await control.evaluate('el => ((el.getAttribute("aria-controls") || "") + " " + (el.getAttribute("aria-owns") || "")).trim().split(/\\s+/).filter(Boolean)')
        owned = []
        for owner_id in owner_ids:
            popup = frame.locator(f'[id={json.dumps(owner_id)}]')
            if await popup.count() == 1 and await popup.is_visible():
                owned.append(popup)
        if len(owned) == 1:
            return control, owned[0]
        if not owner_ids:
            # Never select from several unrelated menus, or from the page body.
            popups = frame.locator('[role="listbox"]:visible, [role="menu"]:visible')
            if await popups.count() == 1:
                return control, popups.first
        if asyncio.get_running_loop().time() >= deadline:
            raise ValueError('No unambiguous popup belongs to this dropdown. Inspect the current state.')
        await asyncio.sleep(0.05)


async def custom_options(popup):
    return await popup.locator(OPTION_SELECTOR).evaluate_all(r'''elements => elements.flatMap((el, index) => {
        const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
        if (!rect.width || !rect.height || style.display === 'none' || style.visibility === 'hidden') return [];
        return [{index, text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
            value: el.getAttribute('data-value') || '', selected: el.getAttribute('aria-selected') === 'true',
            disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true'}];
    })''')


async def select_custom_option(session, node: EnhancedDOMTreeNode, text: str) -> None:
    control, popup = await custom_dropdown(session, node)
    expected = ' '.join(text.split())
    matches = [option for option in await custom_options(popup) if option['text'] == expected]
    if len(matches) != 1 or matches[0]['disabled']:
        raise ValueError(f'Expected one enabled exact option {text!r}; found {len(matches)}. Inspect dropdown_options.')
    option = popup.locator(OPTION_SELECTOR).nth(matches[0]['index'])
    interactions = session.action_watchdog.interactions
    async with interactions._lock:
        # Scroll the option inside its menu, including options clipped below it.
        await option.scroll_into_view_if_needed(timeout=2000)
        await interactions._wait_for_click_ready(option)
        await interactions.input.click(option, button='left')
        # One activation only. A disappearing popup alone is not proof of selection.
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            state = await control.evaluate(r'''el => ({
                text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
                value: 'value' in el ? String(el.value || '') : '',
                valueText: el.getAttribute('aria-valuetext') || '',
                expanded: el.getAttribute('aria-expanded')
            })''')
            selected = expected in {state['text'], state['value'], state['valueText']}
            if not selected and await option.count():
                selected = await option.get_attribute('aria-selected') == 'true'
            if selected:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f'Option click was sent once, but selection of {text!r} is unconfirmed.')
            await asyncio.sleep(0.05)

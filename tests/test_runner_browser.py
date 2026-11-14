"""Runner verification with real Firefox operations and deterministic model output."""

import unittest
from urllib.parse import quote

from browser_use.agent.service import Agent
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.tools.service import Tools

from camoufox_browseruse import open_camoufox_session, register_camoufox_tools
from camoufox_browseruse.runner import BrowserTaskRunner, RunLimits


class TestRunnerBrowser(unittest.IsolatedAsyncioTestCase):
    async def test_pause_resume_canvas_async_evaluation_and_form_completion(self):
        async with open_camoufox_session(headless=True, humanize=False) as session:
            page = await session.get_current_page()
            await page.goto('data:text/html,' + quote('''
                <canvas id="drawing" width="600" height="400"></canvas>
                <form onsubmit="event.preventDefault(); document.getElementById('status').textContent='Submitted'">
                  <label>Name <input id="name"></label><button id="submit">Submit</button>
                </form><div id="status">Ready</div>
                <div id="custom-target" style="cursor:pointer">
                  <span>Custom action</span>
                </div>
            '''))
            await session.get_browser_state_summary()
            self.assertIsNotNone(await session.get_index_by_id('custom-target'))

            class Model:
                model = 'mock-runner'
                model_name = name = model
                provider = 'mock'
                phase = 'stall'
                index = 0
                saw_recovery = False
                image_counts = []

                async def ainvoke(self, messages, output_format=None, **kwargs):
                    self.saw_recovery |= any('Execution feedback:' in message.text for message in messages)
                    images = [part for message in messages if isinstance(message.content, list)
                              for part in message.content if getattr(part, 'type', None) == 'image_url']
                    if len(images) > 1:
                        raise AssertionError(f'Expected at most one current screenshot, got {len(images)}')
                    self.image_counts.append(len(images))
                    if self.phase == 'stall':
                        action = {'evaluate': {'code': '() => null'}}
                    else:
                        self.index += 1
                        if self.index == 1:
                            action = {'evaluate': {'code': '''async () => {
                                await new Promise(resolve => setTimeout(resolve, 50));
                                const ctx = document.getElementById('drawing').getContext('2d');
                                ctx.fillStyle = 'black'; ctx.fillRect(0, 0, 600, 400);
                            }'''}}
                        elif self.index == 2:
                            action = {'input': {'index': await session.get_index_by_id('name'), 'text': 'Ada'}}
                        elif self.index == 3:
                            action = {'click': {'index': await session.get_index_by_id('submit')}}
                        else:
                            if await page.locator('#status').inner_text() != 'Submitted':
                                raise AssertionError('The form did not submit')
                            action = {'done': {'text': 'Observed Submitted', 'success': True}}
                    proposed = [action]
                    if self.phase == 'stall':
                        proposed.append({'click': {'index': await session.get_index_by_id('submit')}})
                    return ChatInvokeCompletion(
                        completion=output_format.model_validate({'memory': 'Retain the form task', 'action': proposed}),
                        usage=None,
                    )

            tools = Tools()
            register_camoufox_tools(tools)
            model = Model()
            agent = Agent(
                task='Fill the visible form', llm=model, tools=tools, browser_session=session,
                use_vision=True, use_judge=False, flash_mode=True, enable_signal_handler=False,
            )
            runner = BrowserTaskRunner(
                agent, RunLimits(max_steps=20, no_effect_window=2, max_recoveries=1), vision_mode='adaptive',
            )
            report = await runner.run()
            self.assertEqual(report.reason, 'stagnation')
            self.assertEqual(report.steps, 4)
            self.assertTrue(all(item.actions == ['evaluate'] for item in report.evidence))
            self.assertTrue(all(item.proposed_actions == ['evaluate', 'click'] for item in report.evidence))
            self.assertTrue(all(item.action_errors == 0 for item in report.evidence))
            self.assertTrue(model.saw_recovery)
            self.assertEqual(model.image_counts[0], 1)
            self.assertIn(0, model.image_counts)
            self.assertFalse(page.is_closed())
            retained = agent.message_manager.state
            history_length = len(agent.history.history)
            model.phase = 'complete'
            report = await runner.run('Continue with the form using the current page')
            self.assertEqual(report.reason, 'done')
            self.assertTrue(report.agent_reported_success)
            self.assertTrue(all(item.action_errors == 0 for item in report.evidence))
            self.assertIs(agent.message_manager.state, retained)
            self.assertGreater(len(agent.history.history), history_length)
            self.assertTrue(report.evidence[0].image_changed)
            self.assertFalse(report.evidence[0].dom_changed)
            self.assertEqual(await page.locator('#name').input_value(), 'Ada')
            self.assertEqual(await page.locator('#status').inner_text(), 'Submitted')
            self.assertFalse(page.is_closed())

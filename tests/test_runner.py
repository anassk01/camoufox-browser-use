"""General progress and bounded-run contracts; no site-specific fixtures."""

import asyncio
import base64
import json
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw
from browser_use.agent.message_manager.views import MessageManagerState
from browser_use.agent.views import ActionResult
from browser_use.filesystem.file_system import FileSystem

from camoufox_browseruse.learning import ExperienceLearner, ProposedLesson, Reflection
from camoufox_browseruse.runner import (
    EXPERIENCE_FILE, LESSONS_FILE, LESSONS_TEMPLATE, BrowserTaskRunner, ProgressTracker, RunLimits,
)


def observation(text='same page', color='white', url='https://example.test', size=(64, 64), box=None, fill=None):
    buffer = BytesIO()
    image = Image.new('RGB', size, color)
    if box:
        ImageDraw.Draw(image).rectangle(box, fill=fill)
    image.save(buffer, format='JPEG' if box else 'PNG')
    return SimpleNamespace(
        dom_state=SimpleNamespace(llm_representation=lambda: text), url=url,
        screenshot=base64.b64encode(buffer.getvalue()).decode(),
    )


class TestProgressTracker(unittest.TestCase):
    def tracker(self):
        tracker = ProgressTracker(RunLimits(no_effect_window=2, max_recoveries=1))
        tracker.observe(observation(), [], [], 0)
        return tracker

    def test_no_effect_recovers_once_then_pauses(self):
        tracker = self.tracker()
        decisions = [tracker.observe(observation(), ['click'], [ActionResult(extracted_content='Clicked')], i).decision
                     for i in range(1, 5)]
        self.assertEqual(decisions, ['continue', 'recover', 'continue', 'pause'])

    def test_reading_new_information_on_unchanged_page_is_activity(self):
        tracker = self.tracker()
        tracker.limits.information_is_progress = True
        for i in range(10):
            item = tracker.observe(observation(), ['read_file'], [ActionResult(extracted_content=f'page {i}')], i)
            self.assertTrue(item.new_information)
            self.assertEqual(item.decision, 'continue')

    def test_reworded_inspection_cannot_prevent_recovery(self):
        tracker = self.tracker()
        decisions = [tracker.observe(observation(), ['evaluate'],
                     [ActionResult(extracted_content=f'new wording {i}')], i).decision for i in range(1, 5)]
        self.assertEqual(decisions, ['continue', 'recover', 'continue', 'pause'])

    def test_visual_only_changes_are_detected(self):
        tracker = self.tracker()
        item = tracker.observe(observation(color='black'), ['click'], [], 1)
        self.assertFalse(item.dom_changed)
        self.assertTrue(item.image_changed)

    def test_small_visual_state_counts_but_caret_blink_does_not(self):
        tracker = ProgressTracker(RunLimits())
        blank = dict(size=(1280, 800))
        caret = dict(blank, box=(300, 100, 301, 117), fill='black')
        tracker.observe(observation(**blank), [], [], 0)
        self.assertFalse(tracker.observe(observation(**caret), ['wait'], [], 1).image_changed)
        # A 28 px tile turning blue is the only sign of selection when state lives in a CSS class.
        tile = dict(blank, box=(600, 400, 627, 427), fill=(66, 133, 244))
        self.assertTrue(tracker.observe(observation(**tile), ['click'], [], 2).image_changed)

    def test_repeated_information_and_cycling_states_do_not_reset_stalls(self):
        tracker = self.tracker()
        tracker.limits.revisit_window = 3
        result = ActionResult(extracted_content='same facts')
        tracker.observe(observation(text='other', color='black'), ['evaluate'], [result], 1)
        tracker.observe(observation(), ['evaluate'], [result], 2)
        item = tracker.observe(observation(text='other', color='black'), ['evaluate'], [result], 3)
        self.assertEqual((item.decision, item.stall), ('continue', None))
        item = tracker.observe(observation(), ['evaluate'], [result], 4)
        self.assertEqual((item.decision, item.stall), ('recover', 'revisiting'))

    def test_retry_after_reset_rewalks_known_states_without_stalling(self):
        tracker = self.tracker()
        walk = [observation(text=f'split {i}', color=(i * 60, 0, 0)) for i in range(1, 4)]
        for step, state in enumerate(walk, 1):
            self.assertTrue(tracker.observe(state, ['click'], [], step).novel_state)
        # Reset to the start, then redo the same splits: each click has an effect.
        for step, state in enumerate([observation(), *walk], 4):
            item = tracker.observe(state, ['click'], [], step)
            self.assertTrue(item.dom_changed)
            self.assertFalse(item.novel_state)
            self.assertEqual(item.decision, 'continue')
        item = tracker.observe(observation(text='split 4', color='blue'), ['click'], [], 8)
        self.assertTrue(item.novel_state)
        self.assertEqual(tracker.stale_steps, 0)

    def test_evaluate_acknowledgement_and_failed_extraction_are_not_progress(self):
        tracker = self.tracker()
        tracker.observe(observation(), ['evaluate'], [ActionResult(extracted_content='Executed script successfully: None')], 1)
        item = tracker.observe(observation(), ['extract'], [ActionResult(error='failed', extracted_content='new')], 2)
        self.assertEqual(item.decision, 'recover')
        self.assertFalse(item.new_information)


def feedback_messages(agent):
    return [item.system_message for item in agent.message_manager.state.agent_history_items
            if (item.system_message or '').startswith('Execution feedback')]


class FakeAgent:
    def __init__(self):
        self.state = SimpleNamespace(n_steps=1, last_model_output=None, last_result=[], consecutive_failures=0, stopped=False)
        self.message_manager = SimpleNamespace(state=MessageManagerState(), max_history_items=None)
        self.tokens = 0
        self.delay = 0
        self.observation = observation()
        self.browser_session = SimpleNamespace(
            browser_profile=SimpleNamespace(keep_alive=True), get_browser_state_summary=self.get_state,
        )
        self.token_cost_service = SimpleNamespace(get_usage_summary=self.get_usage)
        self.history = SimpleNamespace(is_done=lambda: False, is_successful=lambda: None)
        self.run_limits = []
        self.followups = []

    async def get_state(self, **kwargs):
        return self.observation

    async def get_usage(self):
        return SimpleNamespace(total_tokens=self.tokens)

    def stop(self):
        self.state.stopped = True

    def add_new_task(self, text):
        self.followups.append(text)
        self.state.stopped = False

    async def run(self, max_steps, on_step_start, on_step_end):
        self.run_limits.append(max_steps)
        while self.state.n_steps <= max_steps and not self.state.stopped:
            await on_step_start(self)
            if self.state.stopped:
                break
            await asyncio.sleep(self.delay)
            self.tokens += 10
            self.state.last_model_output = SimpleNamespace(
                action=[SimpleNamespace(model_dump=lambda **kw: {'click': {'index': 1}})],
                memory='', next_goal='Work on the tile grid',
            )
            self.state.last_result = [ActionResult(extracted_content='Clicked')]
            self.state.n_steps += 1
            await on_step_end(self)


class TestBrowserTaskRunner(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_usage_counts_against_budget_without_double_counting(self):
        agent = FakeAgent()
        agent.llm = SimpleNamespace(rejected_tokens=20)
        async def usage():
            if agent.state.n_steps > 1:
                agent.llm.rejected_tokens = 30
            return SimpleNamespace(total_tokens=agent.tokens)
        agent.token_cost_service.get_usage_summary = usage
        report = await BrowserTaskRunner(agent, RunLimits(max_tokens=20)).run()
        self.assertEqual(report.reason, 'token_budget')
        self.assertEqual(report.total_tokens, 20)
        self.assertEqual(report.steps, 1)

    async def test_adaptive_vision_restores_setting_and_keeps_visual_pages(self):
        for visual, expected in [(True, True), (False, 'auto')]:
            agent = FakeAgent()
            agent.settings = SimpleNamespace(use_vision=True)
            modes = []
            async def evaluate(script):
                return visual
            async def page():
                return SimpleNamespace(evaluate=evaluate)
            async def state(**kwargs):
                modes.append(agent.settings.use_vision)
                return agent.observation
            agent.browser_session.get_current_page = page
            agent.browser_session.get_browser_state_summary = state
            await BrowserTaskRunner(agent, RunLimits(max_steps=1), vision_mode='adaptive').run()
            self.assertTrue(all(mode == expected for mode in modes))
            self.assertIs(agent.settings.use_vision, True)

    async def test_runner_bounds_history_during_run_and_restores_caller_setting(self):
        agent = FakeAgent()
        observed_limits = []
        original_run = agent.run

        async def run(*args, **kwargs):
            observed_limits.append(agent.message_manager.max_history_items)
            await original_run(*args, **kwargs)

        agent.run = run
        await BrowserTaskRunner(agent, RunLimits(max_steps=1, max_history_items=8)).run()
        self.assertEqual(observed_limits, [8])
        self.assertIsNone(agent.message_manager.max_history_items)

    async def test_adaptive_vision_drops_unchanged_inspection_screenshot(self):
        agent = FakeAgent()
        agent.settings = SimpleNamespace(use_vision=True)
        modes = []

        async def page():
            return SimpleNamespace(evaluate=lambda _script: asyncio.sleep(0, result=True))

        async def state(**kwargs):
            modes.append(agent.settings.use_vision)
            return agent.observation

        async def run(max_steps, on_step_start, on_step_end):
            while agent.state.n_steps <= max_steps and not agent.state.stopped:
                await on_step_start(agent)
                agent.tokens += 10
                agent.state.last_model_output = SimpleNamespace(
                    action=[SimpleNamespace(model_dump=lambda **kw: {'evaluate': {'code': '() => 1'}})]
                )
                agent.state.last_result = [ActionResult(
                    extracted_content='Executed script successfully: 1',
                    include_extracted_content_only_once=True,
                )]
                agent.state.n_steps += 1
                await on_step_end(agent)

        agent.browser_session.get_current_page = page
        agent.browser_session.get_browser_state_summary = state
        agent.run = run
        await BrowserTaskRunner(agent, RunLimits(max_steps=2), vision_mode='adaptive').run()
        self.assertIn(True, modes)
        self.assertIn('auto', modes)

    async def test_stall_preserves_agent_memory_and_browser_for_explicit_continuation(self):
        agent = FakeAgent()
        runner = BrowserTaskRunner(agent, RunLimits(no_effect_window=2, max_recoveries=1))
        report = await runner.run()
        self.assertEqual(report.reason, 'stagnation')
        self.assertEqual(report.steps, 4)
        retained = agent.message_manager.state
        self.assertEqual(len(retained.agent_history_items), 2)
        await runner.run('Continue after I fixed the page')
        self.assertIs(agent.message_manager.state, retained)
        self.assertEqual(agent.followups, ['Continue after I fixed the page'])
        self.assertEqual(agent.run_limits, [100, 104])

    async def test_lessons_notebook_is_shown_every_step_and_requested_on_recovery(self):
        agent = FakeAgent()
        with tempfile.TemporaryDirectory() as directory:
            agent.file_system = FileSystem(directory)
            runner = BrowserTaskRunner(agent, RunLimits(no_effect_window=2, max_recoveries=1))
            await runner.run()
            # Upstream prints small files in full in every step's <file_system> state.
            self.assertIn(LESSONS_TEMPLATE.strip(), agent.file_system.describe())
            feedback = feedback_messages(agent)
            self.assertIn(LESSONS_FILE, feedback[0])
            self.assertNotIn('only inspected', feedback[0])
            await agent.file_system.write_file(LESSONS_FILE, 'Clicks split boxes until minimum size.')
            await runner.run('Continue')
            self.assertEqual(agent.file_system.display_file(LESSONS_FILE), 'Clicks split boxes until minimum size.')

    async def test_learner_recalls_experience_and_judges_outcomes_externally(self):
        with tempfile.TemporaryDirectory() as directory:
            learner = ExperienceLearner(Path(directory) / 'experience')
            proposal = ProposedLesson(kind='strategy', scope='tile grid', text='Clicking a tile splits it into four.')

            class Reflector:
                calls = []

                async def ainvoke(self, messages, output_format=None):
                    self.calls.append(messages[1].content)
                    return SimpleNamespace(completion=Reflection(lessons=[proposal]))

            async def run_once(probe=None, notes=None):
                agent = FakeAgent()
                agent.task = 'Solve level 9'
                agent.llm = Reflector()
                agent.observation = observation(url='https://Neal.fun/not-a-robot/')
                agent.file_system = FileSystem(Path(directory) / f'agent-{time.time_ns()}')
                if notes:
                    await agent.file_system.write_file(LESSONS_FILE, notes)
                await BrowserTaskRunner(
                    agent, RunLimits(max_steps=2, no_effect_window=2, max_recoveries=0), progress_probe=probe,
                    learner=learner,
                ).run()
                return agent.file_system.display_file(EXPERIENCE_FILE)

            # First run: nothing to recall; it stalls, so the outcome is a failure.
            self.assertIsNone(await run_once(notes='Agent scratch: clicks split tiles.'))
            self.assertIn('outcome: failed', Reflector.calls[0].lower())
            self.assertIn('goal: Work on the tile grid', Reflector.calls[0])
            self.assertIn('Agent scratch', Reflector.calls[0])
            lesson, = learner.store.load('neal.fun')
            self.assertEqual(lesson.status, 'candidate')
            # Second run sees the hypothesis; a verified probe promotes it.
            async def advanced(agent):
                return True
            recalled = await run_once(probe=advanced)
            self.assertIn('Untested strategy [tile grid]: Clicking a tile', recalled)
            self.assertEqual(learner.store.load('neal.fun')[0].status, 'trusted')
            self.assertIn('Trusted strategy', await run_once())

    async def test_inspection_only_stall_asks_for_an_interaction(self):
        agent = FakeAgent()

        async def run(max_steps, on_step_start, on_step_end):
            while agent.state.n_steps <= max_steps and not agent.state.stopped:
                await on_step_start(agent)
                agent.state.last_model_output = SimpleNamespace(
                    action=[SimpleNamespace(model_dump=lambda **kw: {'evaluate': {'code': '() => 1'}})])
                agent.state.last_result = [ActionResult(extracted_content='Executed script successfully: 1')]
                agent.state.n_steps += 1
                await on_step_end(agent)

        agent.run = run
        await BrowserTaskRunner(agent, RunLimits(no_effect_window=2, max_recoveries=1)).run()
        self.assertIn('only inspected', feedback_messages(agent)[0])

    async def test_steps_and_token_budget_do_not_claim_success(self):
        for limits, expected in [(RunLimits(max_steps=1), 'step_budget'), (RunLimits(max_tokens=10), 'token_budget')]:
            report = await BrowserTaskRunner(FakeAgent(), limits).run()
            self.assertEqual(report.reason, expected)
            self.assertIsNone(report.agent_reported_success)
            self.assertEqual(report.steps, 1)

    async def test_external_progress_probe_supports_non_page_work(self):
        async def verified_file_write(agent):
            return True

        runner = BrowserTaskRunner(
            FakeAgent(), RunLimits(max_steps=6, no_effect_window=2, max_recoveries=0),
            progress_probe=verified_file_write,
        )
        report = await runner.run()
        self.assertEqual(report.reason, 'step_budget')
        self.assertEqual(report.recoveries, 0)
        self.assertTrue(all(row.verified_progress for row in report.evidence))

    async def test_deadline_cancels_active_step(self):
        agent = FakeAgent()
        agent.delay = 10
        runner = BrowserTaskRunner(agent, RunLimits(max_seconds=0.03))
        report = await runner.run()
        self.assertEqual(report.reason, 'time_budget')
        self.assertTrue(agent.state.stopped)

    async def test_deadline_also_bounds_initial_usage_lookup(self):
        agent = FakeAgent()

        async def stalled_usage():
            await asyncio.Event().wait()

        agent.token_cost_service.get_usage_summary = stalled_usage
        report = await BrowserTaskRunner(agent, RunLimits(max_seconds=0.03)).run()
        self.assertEqual(report.reason, 'time_budget')
        self.assertEqual(report.steps, 0)

    async def test_observation_failure_does_not_become_stagnation(self):
        agent = FakeAgent()

        async def broken(**kwargs):
            raise RuntimeError('page disappeared')

        agent.browser_session.get_browser_state_summary = broken
        report = await BrowserTaskRunner(agent).run()
        self.assertEqual(report.reason, 'observation_failed')
        self.assertEqual(report.steps, 0)

    async def test_evidence_artifact_contains_no_raw_page_or_action_content(self):
        agent = FakeAgent()
        agent.observation = observation(text='secret text')
        with tempfile.TemporaryDirectory() as directory:
            runner = BrowserTaskRunner(agent, RunLimits(max_steps=1), artifact_dir=Path(directory))
            await runner.run()
            path, = Path(directory).glob('*.json')
            raw = path.read_text()
            self.assertNotIn('secret text', raw)
            self.assertNotIn('example.test', raw)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(raw)['reason'], 'step_budget')


if __name__ == '__main__':
    unittest.main()

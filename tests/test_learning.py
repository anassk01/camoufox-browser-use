"""Outcome-gated, scope-credited experience learning; no browser required."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from camoufox_browseruse.learning import (
    MAX_RECALL_CHARS, Episode, ExperienceLearner, ProposedLesson, Reflection, StepContext,
)

ROTATE = ProposedLesson(kind='strategy', scope='Level 5 Rotation', text='Clicking a tile rotates it by 90 degrees.')
SOURCE = ProposedLesson(kind='pitfall', scope='Level 5 Rotation', text='Inspecting page scripts wasted steps.')
LEVEL_5 = 'Level 5: Rotation. Rotate tiles to reassemble the image.'
LEVEL_4 = 'Level 4: Vegetables. Select the vegetables.'


def episode(outcome='failed', recalled=(), contexts=(), stop='stagnation', task='Solve the levels'):
    return Episode(
        site='neal.fun', task=task, outcome=outcome, stop_reason=stop, steps=len(contexts),
        recalled_ids=list(recalled), contexts=[StepContext(goal=g, verified=v) for g, v in contexts],
    )


class FakeLLM:
    def __init__(self, lessons=(), error=None):
        self.lessons, self.error, self.calls = list(lessons), error, []

    async def ainvoke(self, messages, output_format=None):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return SimpleNamespace(completion=Reflection(lessons=self.lessons))


class ExperienceLearnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.learner = ExperienceLearner(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def shown(self):
        return self.learner.recall('neal.fun', 'Solve the levels')

    def lesson(self):
        lesson, = self.learner.store.load('neal.fun')
        return lesson

    async def test_only_verified_progress_inside_the_scope_promotes(self):
        await self.learner.learn(episode(), FakeLLM([ROTATE]))
        text, ids = self.shown()
        self.assertIn('Untested strategy [Level 5 Rotation]: Clicking a tile', text)
        # Progress on another level, or the agent's own claim, is not credit for a level-5 lesson.
        self.learner.apply_outcome(episode('verified', ids, [(LEVEL_4, True), (LEVEL_4, False)], stop='done'), [])
        self.learner.apply_outcome(episode('claimed', ids, [(LEVEL_5, False)], stop='done'), [])
        self.assertEqual((self.lesson().status, self.lesson().wins, self.lesson().failures), ('candidate', 0, 0))
        self.learner.apply_outcome(episode('failed', ids, [(LEVEL_5, False), (LEVEL_5, True), (LEVEL_4, False)]), [])
        self.assertEqual(self.lesson().status, 'trusted')
        self.assertIn('Trusted strategy', self.shown()[0])

    async def test_ending_stuck_in_scope_demotes_then_retires(self):
        self.learner.apply_outcome(episode(), [SOURCE])
        _, ids = self.shown()
        self.learner.apply_outcome(episode('failed', ids, [(LEVEL_5, True)]), [])
        # Stuck elsewhere is not the lesson's fault.
        self.learner.apply_outcome(episode('failed', ids, [(LEVEL_5, False), (LEVEL_4, False)]), [])
        self.assertEqual(self.lesson().failures, 0)
        for _ in range(2):
            self.learner.apply_outcome(episode('failed', ids, [(LEVEL_4, True), (LEVEL_5, False)]), [])
        # Level-4 progress earns a level-5 lesson nothing; ending stuck on level 5 counts against it.
        self.assertEqual((self.lesson().status, self.lesson().wins, self.lesson().failures), ('candidate', 1, 2))
        for _ in range(2):
            self.learner.apply_outcome(episode('failed', ids, [(LEVEL_5, False)], stop='observation_failed'), [])
        self.assertEqual(self.lesson().status, 'retired')
        self.assertEqual(self.shown(), ('', []))

    async def test_task_wide_scopes_become_site_wide_and_are_never_blamed(self):
        broad = ProposedLesson(kind='pitfall', scope='puzzle levels', text='Interact instead of reading app state.')
        stage = ProposedLesson(kind='strategy', scope='Level 9 Nested', text='Subdivide tiles over the object.')
        run = [(f'Solve the puzzle levels, now Level {n}', False) for n in range(1, 6)]
        task = 'Solve all the puzzle levels on the page.'
        # A whole run spent on one stage keeps that stage's scope; a scope restating the task does not.
        self.learner.apply_outcome(episode('failed', contexts=[('Level 9: Nested', False)] * 5, task=task), [stage])
        self.assertEqual(self.lesson().scope, 'Level 9 Nested')
        self.learner.store.save('neal.fun', [])
        self.learner.apply_outcome(episode('failed', contexts=run, task=task), [broad])
        self.assertEqual(self.lesson().scope, '')
        text, ids = self.shown()
        self.assertIn('[site-wide]', text)
        for _ in range(5):
            self.learner.apply_outcome(episode('failed', ids, run), [])
        self.assertEqual((self.lesson().status, self.lesson().failures), ('candidate', 0))
        self.learner.apply_outcome(episode('verified', ids, [(LEVEL_4, True)], stop='done'), [])
        self.assertEqual(self.lesson().status, 'trusted')

    async def test_recall_is_budgeted_ranked_and_deduplicated(self):
        many = [ProposedLesson(kind='pitfall', scope='Level 9 Nested', text=' '.join(f'w{i}t{j}' for j in range(25)))
                for i in range(9)]
        for start in range(0, 9, 3):
            self.learner.apply_outcome(episode(), many[start:start + 3])
        self.learner.apply_outcome(episode(), [ProposedLesson(kind='pitfall', scope='x', text=many[0].text.upper())])
        self.assertEqual(len(self.learner.store.load('neal.fun')), 9)
        self.learner.apply_outcome(episode(), [
            ProposedLesson(kind='pitfall', scope='x', text='Avoid using evaluate calls to inspect Vue state; click the tiles.'),
            ProposedLesson(kind='pitfall', scope='x', text='Avoid repeated evaluate calls to inspect Vue state; click tiles directly.'),
        ])
        self.assertEqual(len(self.learner.store.load('neal.fun')), 10)
        text, ids = self.shown()
        self.assertLessEqual(len(text), MAX_RECALL_CHARS)
        self.assertLess(len(ids), 9)
        self.learner.apply_outcome(episode('verified', [ids[-1]], [('Level 9: Nested grid', True)], stop='done'), [])
        self.assertEqual(self.shown()[1][0], ids[-1])

    async def test_store_is_owner_only_and_rejects_unsafe_site_names(self):
        self.learner.apply_outcome(episode(), [ROTATE])
        path, = Path(self.directory.name).iterdir()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.learner.apply_outcome(Episode(site='../etc', task='t', outcome='failed', stop_reason='x', steps=1), [ROTATE])
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)

    async def test_reflection_failure_still_records_the_outcome(self):
        self.learner.apply_outcome(episode(), [ROTATE])
        _, ids = self.shown()
        await self.learner.learn(episode('verified', ids, [(LEVEL_5, True)], stop='done'), FakeLLM(error=RuntimeError('down')))
        self.assertEqual(self.lesson().status, 'trusted')

    async def test_reflection_sees_outcome_and_unknown_runs_are_not_reflected(self):
        llm = FakeLLM([ROTATE, SOURCE, ROTATE, SOURCE])
        lessons = await self.learner.learn(episode('claimed', stop='done'), llm)
        self.assertEqual(len(lessons), 2)
        system, user = llm.calls[0]
        self.assertIn('"claimed"', system.content)
        self.assertIn('Solve the levels', user.content)
        await self.learner.learn(episode('unknown', stop='error'), llm)
        self.assertEqual(len(llm.calls), 1)


if __name__ == '__main__':
    unittest.main()

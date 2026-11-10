"""Experience learning outside the browser layer.

After a run, a reflector proposes short lessons from the run's evidence. Each
lesson has a scope, words naming the page or stage it is about. A lesson starts
as an untested hypothesis and becomes trusted only when a later run that was
shown it reached externally verified progress while working in that scope; a
run that ends stuck in that scope counts against it until it is retired.
Progress elsewhere in the run is not credited to it, and the agent's own success
claim never promotes anything, because self-judged memory can poison later runs.

This module knows nothing about Camoufox or Playwright: the runner hands it an
Episode and asks for a short recall text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Outcome = Literal['verified', 'claimed', 'failed', 'unknown']
MAX_LESSON_CHARS = 160
MAX_PROPOSALS = 3
# Browser-Use prints files under 600 characters in full in every step.
MAX_RECALL_CHARS = 560
RETIRE_AFTER_FAILURES = 3
DUPLICATE_OVERLAP = 0.5
STUCK_REASONS = frozenset({'stagnation', 'step_budget', 'time_budget', 'token_budget', 'observation_failed'})


class Lesson(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    kind: Literal['pitfall', 'strategy']
    text: str
    # Words naming the page or stage, matched against the agent's goal text.
    scope: str = ''
    task: str = ''
    status: Literal['candidate', 'trusted', 'retired'] = 'candidate'
    wins: int = 0
    failures: int = 0
    source_run: str = ''
    created: float = Field(default_factory=time.time)


class StepContext(BaseModel):
    """What the agent said it was working on at one step, and whether progress there was verified."""

    goal: str
    verified: bool = False


class Episode(BaseModel):
    """What the supervisor observed. Never persisted; only derived lessons are stored."""

    site: str
    task: str
    outcome: Outcome
    stop_reason: str
    steps: int
    recalled_ids: list[str] = Field(default_factory=list)
    # Compact per-step trail, e.g. "3 click dom+ img+ stall:no_effect goal: ...".
    trail: list[str] = Field(default_factory=list)
    contexts: list[StepContext] = Field(default_factory=list)
    agent_notes: str = ''
    run_id: str = Field(default_factory=lambda: uuid4().hex[:8])


class ProposedLesson(BaseModel):
    kind: Literal['pitfall', 'strategy']
    scope: str
    text: str


class Reflection(BaseModel):
    lessons: list[ProposedLesson]


REFLECTION_PROMPT = """You review one finished run of a browser agent and write lessons for future runs on the same site.

Rules:
- At most {count} lessons, each one sentence under {chars} characters.
- scope: 2-5 words copied from the trail's goal text that name the ONE page or stage the lesson is
  about (for example a level name or "checkout form"). Lessons are only applied and judged there.
  Use an empty scope for advice that applies to the whole site.
- A lesson describes how this site or task behaves, or which interaction approach worked or wasted steps.
- Only interactions a normal user could do. Never suggest reading application source, bundles, framework
  state, hidden answers, or changing storage.
- Do not restate the task. Do not invent facts the trail does not support. Return an empty list if
  nothing is worth keeping.
- The run ended "{outcome}": "verified" means confirmed progress at the end, "failed" means it ended
  stuck, "claimed" is the agent's unverified word. Steps marked "verified+" had externally confirmed
  progress; prefer strategies that explain those, and pitfalls from where the run got stuck.
"""


def _normalized(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()


def _in_scope(scope: str, goal: str) -> bool:
    words = {word for word in _normalized(scope).split() if len(word) > 1}
    return bool(words) and words <= set(_normalized(goal).split())


def _site_key(site: str) -> str | None:
    site = site.lower()
    return site if re.fullmatch(r'[a-z0-9-]+(\.[a-z0-9-]+)*', site) else None


class LessonStore:
    """One owner-only JSON file per site."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, site: str) -> Path | None:
        key = _site_key(site)
        return self.directory / f'{key}.json' if key else None

    def load(self, site: str) -> list[Lesson]:
        path = self._path(site)
        if path is None or not path.is_file():
            return []
        try:
            return [Lesson.model_validate(item) for item in json.loads(path.read_text(encoding='utf-8'))]
        except (ValueError, TypeError):
            logger.warning('Ignoring unreadable lesson store %s', path)
            return []

    def save(self, site: str, lessons: list[Lesson]) -> None:
        path = self._path(site)
        if path is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'.{time.time_ns()}.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump([lesson.model_dump() for lesson in lessons], stream, indent=1)
        os.replace(temporary, path)


class ExperienceLearner:
    """Recall lessons for a run and learn from its episode afterwards."""

    def __init__(self, directory: Path, reflection_llm: Any = None) -> None:
        self.store = LessonStore(directory)
        # None means the runner passes the agent's own model.
        self.reflection_llm = reflection_llm

    def recall(self, site: str, task: str) -> tuple[str, list[str]]:
        """Return (text for the agent, ids shown). Trusted lessons first, then a few hypotheses."""
        active = [lesson for lesson in self.store.load(site) if lesson.status != 'retired']
        same_task = _normalized(task)
        active.sort(key=lambda lesson: (
            lesson.status != 'trusted', _normalized(lesson.task) != same_task, -lesson.wins, -lesson.created,
        ))
        header = '# Experience from earlier runs on this site (read-only)\n'
        lines, shown, size = [], [], len(header)
        for lesson in active:
            label = 'Trusted' if lesson.status == 'trusted' else 'Untested'
            where = f' [{lesson.scope}]' if lesson.scope else ' [site-wide]'
            line = f'- {label} {lesson.kind}{where}: {lesson.text}\n'
            if size + len(line) > MAX_RECALL_CHARS:
                break
            lines.append(line)
            shown.append(lesson.id)
            size += len(line)
        return (header + ''.join(lines), shown) if lines else ('', [])

    def apply_outcome(self, episode: Episode, proposals: list[ProposedLesson]) -> list[Lesson]:
        lessons = self.store.load(episode.site)
        ended_stuck = episode.outcome == 'failed' or (episode.stop_reason in STUCK_REASONS)
        for lesson in lessons:
            if lesson.id not in episode.recalled_ids or lesson.status == 'retired':
                continue
            # Credit only the scope the lesson is about, not progress elsewhere in the run.
            if not lesson.scope:
                # Site-wide advice cannot be blamed on one stage; it only earns wins from runs that
                # end with verified progress.
                if episode.outcome == 'verified':
                    lesson.wins += 1
                    lesson.status = 'trusted' if lesson.wins > lesson.failures else lesson.status
                continue
            relevant = [step for step in episode.contexts if _in_scope(lesson.scope, step.goal)]
            if any(step.verified for step in relevant):
                lesson.wins += 1
                if lesson.wins > lesson.failures:
                    lesson.status = 'trusted'
            elif ended_stuck and relevant and relevant[-1] is episode.contexts[-1]:
                lesson.failures += 1
                if lesson.failures >= lesson.wins + RETIRE_AFTER_FAILURES:
                    lesson.status = 'retired'
                elif lesson.status == 'trusted' and lesson.failures > lesson.wins:
                    lesson.status = 'candidate'
        known = [set(_normalized(lesson.text).split()) for lesson in lessons]
        for proposal in proposals[:MAX_PROPOSALS]:
            text = ' '.join(proposal.text.split())[:MAX_LESSON_CHARS]
            words = set(_normalized(text).split())
            # ponytail: word-overlap dedupe; use embeddings if paraphrases still pile up.
            if words and not any(len(words & old) >= DUPLICATE_OVERLAP * len(words | old) for old in known):
                known.append(words)
                scope = ' '.join(proposal.scope.split())[:60]
                if _in_scope(scope, episode.task):
                    # A scope that only restates the task names the whole task, not a stage.
                    scope = ''
                lessons.append(Lesson(
                    kind=proposal.kind, text=text, scope=scope, task=episode.task, source_run=episode.run_id,
                ))
        self.store.save(episode.site, lessons)
        return lessons

    async def reflect(self, episode: Episode, llm: Any) -> list[ProposedLesson]:
        from browser_use.llm.messages import SystemMessage, UserMessage

        report = (
            f'Task: {episode.task}\nOutcome: {episode.outcome} (stopped: {episode.stop_reason}, '
            f'{episode.steps} steps)\nStep trail (oldest first):\n' + '\n'.join(episode.trail[-40:]) +
            f'\nAgent notes at the end of the run:\n{episode.agent_notes[:1200] or "(none)"}'
        )
        prompt = REFLECTION_PROMPT.format(count=MAX_PROPOSALS, chars=MAX_LESSON_CHARS, outcome=episode.outcome)
        response = await llm.ainvoke([SystemMessage(content=prompt), UserMessage(content=report)], output_format=Reflection)
        return response.completion.lessons[:MAX_PROPOSALS]

    async def learn(self, episode: Episode, llm: Any) -> list[Lesson]:
        proposals: list[ProposedLesson] = []
        model = self.reflection_llm or llm
        if model is not None and episode.outcome != 'unknown':
            try:
                proposals = await self.reflect(episode, model)
            except Exception as exc:
                # Outcome bookkeeping must still happen when reflection fails.
                logger.warning(f'Reflection failed: {type(exc).__name__}: {exc}')
        return self.apply_outcome(episode, proposals)

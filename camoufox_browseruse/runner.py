"""Optional, bounded execution policy around the pinned Browser-Use Agent.

Observation changes and new tool output are evidence of activity, not proof of
task success. The original Agent remains responsible for planning and memory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from PIL import Image, ImageChops
from pydantic import BaseModel, Field

from browser_use.agent.message_manager.views import HistoryItem

from camoufox_browseruse.learning import STUCK_REASONS, Episode, ExperienceLearner, Outcome, StepContext

logger = logging.getLogger(__name__)

BROWSER_OPERATION_INSTRUCTIONS = (
    'Operate through rendered page content and normal browser interactions. '
    'Use JavaScript to inspect rendered DOM or geometry when needed. '
    'Do not inspect application bundles, framework internals, hidden answer data, '
    'or change application storage to skip the task. '
    'Distinguish an action being delivered from its intended effect being observed. '
    'Browser-Use element indices are different from DOM query array offsets. '
    'An evaluate action ends its action sequence; request subsequent actions on the next step. '
    'Evaluate code must return quickly: bound every loop, because a script that never returns '
    'freezes the page and ends the run. '
    'After an ineffective approach, inspect a fresh observation and choose a different approach.'
)


INFORMATIONAL_ACTIONS = frozenset({
    'evaluate', 'extract', 'read_file', 'find_elements', 'search_page', 'get_dropdown_options',
})
# Browser-Use shows files under 600 characters in full in every step's state, so
# this notebook survives history trimming without extra prompt plumbing. It is the
# agent's scratchpad for this run; the learner reads it but never stores it as-is.
LESSONS_FILE = 'lessons.md'
LESSONS_TEMPLATE = '# Lessons (shown every step; keep under 600 chars)\n## Verified\n## Failed\n## Next untried\n'
# Read-only recall from ExperienceLearner, rewritten at the start of each run.
EXPERIENCE_FILE = 'experience.md'


class RunLimits(BaseModel):
    max_steps: int = Field(default=100, gt=0)
    max_seconds: float = Field(default=900, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    no_effect_window: int = Field(default=5, ge=2)
    # Steps that change the page but only revisit earlier states, e.g. toggling
    # or redoing work after a reset. Longer than no_effect_window so a retry
    # can re-walk known states before reaching new ones.
    revisit_window: int = Field(default=15, ge=2)
    max_recoveries: int = Field(default=2, ge=0)
    observation_timeout: float = Field(default=15, gt=0)
    information_is_progress: bool = False
    max_history_items: int | None = Field(default=12, ge=6)


class StepEvidence(BaseModel):
    step: int
    actions: list[str]
    proposed_actions: list[str] = Field(default_factory=list)
    # *_changed compare with the previous observation: did the step have an effect?
    dom_changed: bool = False
    image_changed: bool = False
    url_changed: bool = False
    # Not seen earlier in this run: did the step reach somewhere new?
    novel_state: bool = False
    new_information: bool = False
    verified_progress: bool = False
    action_errors: int = 0
    result_chars: int = 0
    screenshot_available: bool = False
    total_tokens: int = 0
    duration_ms: int = 0
    decision: Literal['continue', 'recover', 'pause'] = 'continue'
    stall: Literal['no_effect', 'revisiting'] | None = None


class RunReport(BaseModel):
    reason: Literal[
        'done', 'step_budget', 'time_budget', 'token_budget', 'stagnation',
        'observation_failed', 'agent_stopped', 'error', 'cancelled',
    ]
    steps: int
    recoveries: int
    total_tokens: int
    elapsed_seconds: float
    # This is the Agent's claim. A domain-independent runner cannot verify every task goal.
    agent_reported_success: bool | None = None
    evidence: list[StepEvidence] = Field(default_factory=list)


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _image_signature(screenshot: str | None) -> Image.Image | None:
    if not screenshot:
        return None
    with Image.open(BytesIO(base64.b64decode(screenshot))) as image:
        # Area averaging over ~10x6 px cells (at 1280x800) removes compression noise
        # but keeps a small selection or highlight visible.
        return image.convert('RGB').resize((128, 128), Image.Resampling.BOX)


def _images_differ(image: Image.Image, prior: Image.Image) -> bool:
    red, green, blue = ImageChops.difference(image, prior).split()
    strongest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    # A blinking caret moves 3-4 cells; a ~28 px selection moves about ten.
    return strongest.point(lambda value: 255 if value > 16 else 0).histogram()[255] >= 8


class ProgressTracker:
    """Bounded novelty tracking across DOM, visual state and informational results."""

    def __init__(self, limits: RunLimits) -> None:
        self.limits = limits
        self.dom_seen: deque[str] = deque(maxlen=64)
        self.images_seen: deque[Image.Image] = deque(maxlen=64)
        self.results_seen: deque[str] = deque(maxlen=128)
        self.urls_seen: deque[str] = deque(maxlen=64)
        self.no_effect_steps = 0
        self.stale_steps = 0
        self.recoveries = 0

    def observe(
        self, state: Any, actions: list[str], results: list[Any], step: int, *, verified_progress: bool = False,
    ) -> StepEvidence:
        dom = _digest(state.dom_state.llm_representation())
        url = _digest(state.url)
        image = _image_signature(state.screenshot)
        initialized = bool(self.dom_seen)
        evidence = StepEvidence(
            step=step, actions=actions,
            dom_changed=initialized and dom != self.dom_seen[-1],
            url_changed=initialized and url != self.urls_seen[-1],
            screenshot_available=image is not None,
            verified_progress=verified_progress,
        )
        novel = initialized and (dom not in self.dom_seen or url not in self.urls_seen)
        if image is not None:
            if initialized and self.images_seen:
                evidence.image_changed = _images_differ(image, self.images_seen[-1])
                novel = novel or all(_images_differ(image, prior) for prior in self.images_seen)
            self.images_seen.append(image)
        evidence.novel_state = novel
        self.dom_seen.append(dom)
        self.urls_seen.append(url)

        for index, result in enumerate(results):
            evidence.action_errors += int(bool(result.error))
            content = result.extracted_content or ''
            evidence.result_chars += len(content)
            action = actions[index] if index < len(actions) else None
            if action not in INFORMATIONAL_ACTIONS or result.error:
                continue
            # A generic delivery acknowledgement is not new information.
            content = content.removeprefix('Executed script successfully:').strip()
            if content in {'', 'None', 'null', 'undefined', '{}', '[]', 'True', 'False', 'true', 'false'}:
                continue
            signature = _digest(content)
            if signature not in self.results_seen:
                evidence.new_information = True
                self.results_seen.append(signature)

        if not initialized:
            return evidence
        progress = (self.limits.information_is_progress and evidence.new_information) or evidence.verified_progress
        effect = evidence.dom_changed or evidence.url_changed or evidence.image_changed or progress
        self.no_effect_steps = 0 if effect else self.no_effect_steps + 1
        self.stale_steps = 0 if novel or progress else self.stale_steps + 1
        if self.no_effect_steps >= self.limits.no_effect_window:
            evidence.stall = 'no_effect'
        elif self.stale_steps >= self.limits.revisit_window:
            evidence.stall = 'revisiting'
        if evidence.stall:
            if self.recoveries < self.limits.max_recoveries:
                self.recoveries += 1
                self.no_effect_steps = self.stale_steps = 0
                evidence.decision = 'recover'
            else:
                evidence.decision = 'pause'
        return evidence


class BrowserTaskRunner:
    """Run an existing Agent with budgets and evidence, retaining it for continuation.

    The caller owns the browser and must keep its context open with keep_alive=True.
    Calling run again explicitly allocates a new budget and uses add_new_task to
    continue the same Agent, memory, history, files and browser session.
    """

    def __init__(
        self, agent: Any, limits: RunLimits | None = None, *, artifact_dir: Path | None = None,
        progress_probe: Callable[[Any], Awaitable[bool]] | None = None,
        vision_mode: Literal['always', 'adaptive'] = 'always',
        learner: ExperienceLearner | None = None,
    ) -> None:
        if not agent.browser_session.browser_profile.keep_alive:
            raise ValueError('BrowserTaskRunner requires a caller-owned browser with keep_alive=True.')
        self.agent = agent
        self.limits = limits or RunLimits()
        self.artifact_dir = artifact_dir
        self.progress_probe = progress_probe
        if vision_mode not in {'always', 'adaptive'}:
            raise ValueError('vision_mode must be always or adaptive')
        self.vision_mode = vision_mode
        # Opt-in. Outcomes are judged by progress_probe, never by the agent's own claim.
        self.learner = learner
        self._site: str | None = None
        self._recalled_ids: list[str] = []
        # The agent's stated memory and goal per step; kept in memory for learning, never written to reports.
        self._goals: list[str] = []
        self.last_report: RunReport | None = None
        self._running = False

    async def run(self, continuation: str | None = None) -> RunReport:
        if self._running:
            raise RuntimeError('This runner is already running.')
        self._running = True
        started = time.monotonic()
        tracker = ProgressTracker(self.limits)
        evidence: list[StepEvidence] = []
        reason = 'agent_stopped'
        token_count = 0
        start_step = self.agent.state.n_steps
        initial_tokens = 0
        step_started = started
        original_vision = getattr(getattr(self.agent, 'settings', None), 'use_vision', None)
        message_manager = getattr(self.agent, 'message_manager', None)
        original_history_limit = getattr(message_manager, 'max_history_items', None)

        if message_manager is not None and self.limits.max_history_items is not None:
            if original_history_limit is None or original_history_limit > self.limits.max_history_items:
                message_manager.max_history_items = self.limits.max_history_items

        async def observe() -> Any:
            async with asyncio.timeout(self.limits.observation_timeout):
                state = await self.agent.browser_session.get_browser_state_summary(include_screenshot=True)
            await self._recall(file_system, state.url)
            return state

        async def on_start(agent: Any) -> None:
            nonlocal step_started, reason
            step_started = time.monotonic()
            if self.vision_mode == 'adaptive':
                # Browser-Use's auto mode keeps explicitly requested images. A
                # current screenshot is otherwise useful after interactions or
                # observed page changes, but unchanged inspection loops should
                # not resend the same large image indefinitely.
                previous = evidence[-1] if evidence else None
                mutating_actions = {
                    'click', 'input', 'scroll', 'drag_and_drop', 'navigate',
                    'go_back', 'switch_tab', 'close_tab', 'upload_file',
                    'select_dropdown_option', 'send_keys', 'wait',
                }
                if previous is None:
                    try:
                        async with asyncio.timeout(self.limits.observation_timeout):
                            page = await agent.browser_session.get_current_page()
                            visual = await page.evaluate('''() => Array.from(document.querySelectorAll('*')).some(el => {
                                const r = el.getBoundingClientRect();
                                const s = getComputedStyle(el);
                                return r.width > 16 && r.height > 16 && r.bottom > 0 && r.right > 0
                                    && r.top < innerHeight && r.left < innerWidth
                                    && s.display !== 'none' && s.visibility !== 'hidden'
                                    && (el.matches('canvas,img,svg,video,iframe') || s.backgroundImage !== 'none');
                            })''')
                        agent.settings.use_vision = True if visual else 'auto'
                    except Exception:
                        agent.settings.use_vision = True
                else:
                    needs_fresh_image = (
                        previous.decision == 'recover'
                        or previous.dom_changed
                        or previous.image_changed
                        or previous.url_changed
                        or any(action in mutating_actions for action in previous.actions)
                    )
                    agent.settings.use_vision = True if needs_fresh_image else 'auto'
            if not tracker.dom_seen:
                try:
                    tracker.observe(await observe(), [], [], agent.state.n_steps)
                except Exception:
                    reason = 'observation_failed'
                    agent.stop()

        async def on_end(agent: Any) -> None:
            nonlocal reason, token_count
            usage = await agent.token_cost_service.get_usage_summary()
            token_count = max(0, usage.total_tokens + rejected_tokens() - initial_tokens)
            output = agent.state.last_model_output
            proposed = [next(iter(a.model_dump(exclude_none=True)), 'unknown') for a in output.action] if output else []
            results = agent.state.last_result or []
            # Failed/unexecuted proposals must not be reported as completed tools.
            actions = [name for name, result in zip(proposed, results) if not result.error]
            try:
                async with asyncio.timeout(self.limits.observation_timeout):
                    verified = await self.progress_probe(agent) if self.progress_probe else False
                    item = tracker.observe(
                        await observe(), proposed[:len(results)], results, agent.state.n_steps - 1,
                        verified_progress=verified,
                    )
            except Exception:
                reason = 'observation_failed'
                agent.stop()
                return
            item.total_tokens = token_count
            item.proposed_actions = proposed
            item.actions = actions
            item.duration_ms = round((time.monotonic() - step_started) * 1000)
            evidence.append(item)
            self._goals.append(' '.join(
                str(text) for field in ('memory', 'next_goal') if (text := getattr(output, field, None))
            )[:300] if output else '')
            logger.info('Browser task evidence: %s', item.model_dump())
            if agent.history.is_done():
                reason = 'done'
                return
            if self.limits.max_tokens is not None and token_count >= self.limits.max_tokens:
                reason = 'token_budget'
                agent.stop()
            elif item.decision == 'pause':
                reason = 'stagnation'
                agent.stop()
            elif item.decision == 'recover':
                window = self.limits.no_effect_window if item.stall == 'no_effect' else self.limits.revisit_window
                names = sorted({name for row in evidence[-window:] for name in row.actions})
                observed = (
                    'produced no qualifying progress in the observation window. Reworded inspection output alone '
                    'does not establish progress. '
                ) if item.stall == 'no_effect' else (
                    'changed the page but only returned to states already seen in this run. '
                )
                if names and set(names) <= INFORMATIONAL_ACTIONS:
                    observed += (
                        'These steps only inspected the page. Form a hypothesis from the screenshot and test it '
                        'with a real interaction. '
                    )
                instruction = (
                    f'Execution feedback: the last {window} steps using {", ".join(names)} {observed}'
                    'This is not proof of failure. Use the next fresh observation to verify the intended effect. '
                    f'Before acting, update {LESSONS_FILE}: what you verified, what failed and why, and one '
                    'strategy you have not tried yet that stays within your operating instructions. '
                    'Then carry out that strategy. '
                    'Do not repeat the same inspection with cosmetic changes or infer success from delivery.'
                )
                # The pinned public message-manager state is retained through compaction.
                agent.message_manager.state.agent_history_items.append(HistoryItem(system_message=instruction))

        def rejected_tokens() -> int:
            models = [getattr(self.agent, 'llm', None)]
            settings = getattr(self.agent, 'settings', None)
            models.append(getattr(settings, 'page_extraction_llm', None))
            compaction = getattr(settings, 'message_compaction', None)
            models.append(getattr(compaction, 'compaction_llm', None))
            return sum(getattr(model, 'rejected_tokens', 0) for model in {id(m): m for m in models if m is not None}.values())

        file_system = getattr(self.agent, 'file_system', None)
        if file_system is not None and file_system.get_file(LESSONS_FILE) is None:
            await file_system.write_file(LESSONS_FILE, LESSONS_TEMPLATE)

        try:
            async with asyncio.timeout(self.limits.max_seconds):
                initial_tokens = (await self.agent.token_cost_service.get_usage_summary()).total_tokens + rejected_tokens()
                if self.last_report is not None or continuation:
                    self.agent.add_new_task(continuation or 'Continue the original task from the current browser and retained memory.')
                    self.agent.state.consecutive_failures = 0
                await self.agent.run(
                    max_steps=start_step + self.limits.max_steps - 1,
                    on_step_start=on_start, on_step_end=on_end,
                )
            if reason == 'agent_stopped' and self.agent.state.n_steps >= start_step + self.limits.max_steps:
                reason = 'step_budget'
        except TimeoutError:
            reason = 'time_budget'
            self.agent.stop()
        except asyncio.CancelledError:
            reason = 'cancelled'
            self.agent.stop()
            raise
        except Exception:
            reason = 'error'
            raise
        finally:
            if self.vision_mode == 'adaptive' and original_vision is not None:
                self.agent.settings.use_vision = original_vision
            if message_manager is not None:
                message_manager.max_history_items = original_history_limit
            self._running = False
            self.last_report = RunReport(
                reason=reason, steps=max(0, self.agent.state.n_steps - start_step),
                recoveries=tracker.recoveries, total_tokens=token_count,
                elapsed_seconds=round(time.monotonic() - started, 3),
                agent_reported_success=self.agent.history.is_successful() if reason == 'done' else None,
                evidence=evidence,
            )
            if self.artifact_dir is not None:
                self._save_report()
            if reason != 'cancelled':
                await self._learn(file_system)
        return self.last_report

    async def _recall(self, file_system: Any, url: str) -> None:
        if self.learner is None or file_system is None or self._site is not None:
            return
        site = (urlparse(url).hostname or '').lower()
        if not site:
            return
        # ponytail: keyed by the first site seen in the run; key by host+path if apps on one host collide.
        self._site = site
        text, self._recalled_ids = self.learner.recall(site, getattr(self.agent, 'task', ''))
        if text:
            await file_system.write_file(EXPERIENCE_FILE, text)

    def _outcome(self) -> Outcome:
        """How the run ended; partial verified progress is visible per step in the trail."""
        assert self.last_report is not None
        report = self.last_report
        if report.reason in STUCK_REASONS:
            return 'failed'
        if any(item.verified_progress for item in report.evidence):
            return 'verified'
        if report.reason == 'done':
            return 'claimed' if report.agent_reported_success else 'failed'
        return 'unknown'

    async def _learn(self, file_system: Any) -> None:
        if self.learner is None or self._site is None or self.last_report is None:
            return
        report = self.last_report
        trail = [
            f'{item.step} {",".join(item.actions) or "-"}'
            + ''.join(f' {name}+' for name, changed in (
                ('dom', item.dom_changed), ('img', item.image_changed), ('url', item.url_changed),
                ('new', item.novel_state), ('verified', item.verified_progress)) if changed)
            + (f' errors:{item.action_errors}' if item.action_errors else '')
            + (f' stall:{item.stall}' if item.stall else '')
            + (f' goal: {goal[:120]}' if goal else '')
            for item, goal in zip(report.evidence, self._goals[-len(report.evidence):] if report.evidence else [])
        ]
        contexts = [
            StepContext(goal=goal, verified=item.verified_progress)
            for item, goal in zip(report.evidence, self._goals[-len(report.evidence):] if report.evidence else [])
        ]
        episode = Episode(
            site=self._site, task=getattr(self.agent, 'task', ''), outcome=self._outcome(),
            stop_reason=report.reason, steps=report.steps, recalled_ids=self._recalled_ids, trail=trail,
            contexts=contexts,
            agent_notes=(file_system.display_file(LESSONS_FILE) or '') if file_system is not None else '',
        )
        try:
            async with asyncio.timeout(120):
                await self.learner.learn(episode, getattr(self.agent, 'llm', None))
        except Exception as exc:
            logger.warning(f'Learning from the run failed: {type(exc).__name__}: {exc}')

    def _save_report(self) -> None:
        assert self.artifact_dir is not None and self.last_report is not None
        # No raw prompts, page content, screenshots or credentials are saved here.
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f'run-{time.time_ns()}.json'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(self.last_report.model_dump_json(indent=2))

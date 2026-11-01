"""Browser health monitoring and crash reporting through Playwright."""

from __future__ import annotations

import asyncio
import logging

from browser_use.browser.events import (
    BrowserConnectedEvent,
    BrowserErrorEvent,
    BrowserStoppedEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    TargetCrashedEvent,
)

from .base import NativeWatchdog

logger = logging.getLogger(__name__)


class CrashWatchdog(NativeWatchdog):
    """Native counterpart of Browser-Use's CDP CrashWatchdog."""

    HANDLERS = {
        BrowserConnectedEvent: 'on_BrowserConnectedEvent',
        BrowserStoppedEvent: 'on_BrowserStoppedEvent',
        NavigationStartedEvent: 'on_NavigationStartedEvent',
        NavigationCompleteEvent: 'on_NavigationCompleteEvent',
        TargetCrashedEvent: 'on_TargetCrashedEvent',
    }

    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.check_interval_seconds = 5.0
        self.health_timeout_seconds = 2.0
        self.health_failure_threshold = 2
        self._monitoring_task: asyncio.Task[None] | None = None
        self._reported_unresponsive = False
        self._health_target_id: str | None = None
        self._consecutive_health_failures = 0
        self._navigating_targets: set[str] = set()

    @property
    def is_monitoring(self) -> bool:
        return bool(self._monitoring_task and not self._monitoring_task.done())

    async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
        if self.is_monitoring:
            return
        self._monitoring_task = asyncio.create_task(
            self._monitor_loop(),
            name='camoufox-health-monitor',
        )

    async def on_BrowserStoppedEvent(self, event: BrowserStoppedEvent) -> None:
        await self.stop()

    async def on_NavigationStartedEvent(self, event: NavigationStartedEvent) -> None:
        self._navigating_targets.add(event.target_id)

    async def on_NavigationCompleteEvent(self, event: NavigationCompleteEvent) -> None:
        self._navigating_targets.discard(event.target_id)
        self._reported_unresponsive = False
        self._health_target_id = event.target_id
        self._consecutive_health_failures = 0

    async def on_TargetCrashedEvent(self, event: TargetCrashedEvent) -> None:
        target = self.browser_session.session_manager.find_page(event.target_id)
        self.event_bus.dispatch(BrowserErrorEvent(
            error_type='TargetCrash',
            message=f'Target crashed: {event.target_id}',
            details={
                'target_id': event.target_id,
                'url': target.url if target else None,
                'error': event.error,
                'was_agent_focus': self.browser_session.agent_focus_target_id == event.target_id,
            },
        ))
        await self.browser_session.session_manager.recover_from_crash(event.target_id)

    async def stop(self) -> None:
        task = self._monitoring_task
        self._monitoring_task = None
        self._reported_unresponsive = False
        self._health_target_id = None
        self._consecutive_health_failures = 0
        self._navigating_targets.clear()
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _monitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.check_interval_seconds)
                manager = self.browser_session.session_manager
                page = manager.page
                if manager.context is None or page is None or page.is_closed():
                    continue
                target_id = manager.target_id(page)
                if target_id in self._navigating_targets:
                    continue
                await self._check_page_health(page, target_id)
        except asyncio.CancelledError:
            pass

    async def _check_page_health(self, page, target_id: str) -> None:
        """Require repeated missed pings before declaring a page unresponsive."""
        if self._health_target_id != target_id:
            self._health_target_id = target_id
            self._consecutive_health_failures = 0
            self._reported_unresponsive = False
        try:
            await asyncio.wait_for(
                page.evaluate('() => 1 + 1'),
                timeout=self.health_timeout_seconds,
            )
            self._consecutive_health_failures = 0
            self._reported_unresponsive = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if (
                target_id in self._navigating_targets
                or 'execution context was destroyed' in message
                or 'navigation' in message and 'interrupted' in message
            ):
                return
            page_error = f'{type(exc).__name__}: {exc}'.rstrip(': ')
            context = self.browser_session.session_manager.context
            try:
                if context is None:
                    raise RuntimeError('browser context is unavailable')
                await asyncio.wait_for(
                    context.cookies(),
                    timeout=self.health_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as control_exc:
                control_error = (
                    f'{type(control_exc).__name__}: {control_exc}'.rstrip(': ')
                )
            else:
                # A page may intentionally occupy its JavaScript main thread
                # with rendering or proof-of-work. If Firefox still answers a
                # context-level command, the browser connection is healthy.
                self._consecutive_health_failures = 0
                self._reported_unresponsive = False
                logger.debug(
                    'Camoufox page ping was busy but the browser control plane '
                    'remained responsive: %s',
                    page_error,
                )
                return
            self._consecutive_health_failures += 1
            error = f'page={page_error}; control={control_error}'
            if self._consecutive_health_failures < self.health_failure_threshold:
                logger.debug(
                    'Camoufox page missed a transient health check: %s',
                    error,
                )
                return
            if self._reported_unresponsive:
                return
            self._reported_unresponsive = True
            logger.warning('Camoufox page health check failed: %s', error)
            self.event_bus.dispatch(BrowserErrorEvent(
                error_type='BrowserUnresponsive',
                message='The focused Camoufox page did not answer repeated Playwright health checks.',
                details={
                    'target_id': self.browser_session.agent_focus_target_id,
                    'url': page.url,
                    'error': error,
                    'consecutive_failures': self._consecutive_health_failures,
                },
            ))

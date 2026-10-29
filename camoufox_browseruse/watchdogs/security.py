"""URL policy enforcement for Camoufox requests and navigations."""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import logging
from urllib.parse import urlparse, urlsplit

from browser_use.browser.events import BrowserErrorEvent, TabClosedEvent
from playwright.async_api import Page, Route

from .base import NativeWatchdog

logger = logging.getLogger(__name__)


class SecurityWatchdog(NativeWatchdog):
    """Apply Browser-Use domain policy at both action and network boundaries."""

    def __init__(self, browser_session) -> None:
        super().__init__(browser_session)
        self.errors: list[str] = []
        self.pending_popup_urls: list[str] = []

    @staticmethod
    def is_root_domain(domain: str) -> bool:
        return '*' not in domain and '://' not in domain and domain.count('.') == 1

    @staticmethod
    def domain_variants(host: str) -> tuple[str, str]:
        return (host, host[4:]) if host.startswith('www.') else (host, f'www.{host}')

    @staticmethod
    def is_ip_address(host: str) -> bool:
        host = host.lower().strip('[]')
        if host in {'localhost', 'ip6-localhost', 'ip6-loopback', '0.0.0.0'} or host.endswith('.localhost'):
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    @classmethod
    def is_url_match(cls, url: str, host: str, scheme: str, pattern: str) -> bool:
        host = host.lower()
        scheme = scheme.lower()
        if '://' in pattern:
            try:
                parsed_pattern = urlsplit(pattern)
                pattern_host = (parsed_pattern.hostname or '').lower()
                if parsed_pattern.scheme and scheme != parsed_pattern.scheme.lower():
                    return False
                if '*' in pattern_host:
                    if not fnmatch.fnmatch(host, pattern_host):
                        return False
                elif host != pattern_host and not (
                    cls.is_root_domain(pattern_host) and host == f'www.{pattern_host}'
                ):
                    return False
                if parsed_pattern.port is not None and urlsplit(url).port != parsed_pattern.port:
                    return False
                pattern_path = parsed_pattern.path.rstrip('/')
                if pattern_path and pattern_path != '/':
                    url_path = urlsplit(url).path
                    if url_path != pattern_path and not url_path.startswith(pattern_path + '/'):
                        return False
                return True
            except Exception:
                return False
        if '*' in pattern:
            if pattern.startswith('*.'):
                domain = pattern[2:].lower()
                return host == domain or host.endswith('.' + domain)
            return fnmatch.fnmatch(host, pattern.split('/', 1)[0].lower())
        pattern_host = pattern.split('/', 1)[0].lower().split(':')[0]
        return host == pattern_host or (cls.is_root_domain(pattern_host) and host == f'www.{pattern_host}')

    def is_allowed(self, url: str, *, is_navigation: bool = True) -> bool:
        if not url or url in {'about:blank', 'chrome://new-tab-page/', 'chrome://new-tab-page', 'chrome://newtab/'}:
            return True
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme in {'data', 'blob'}:
            return True
        profile = self.browser_session.browser_profile
        allowed = getattr(profile, 'allowed_domains', None)
        prohibited = getattr(profile, 'prohibited_domains', None)
        if parsed.scheme == 'file':
            if not allowed and not prohibited:
                return True
            return bool(allowed and ('file' in allowed or any('file:' in str(item) for item in allowed)))
        host = parsed.hostname
        if not host:
            return False
        if getattr(profile, 'block_ip_addresses', False) and self.is_ip_address(host):
            return False
        if prohibited:
            if isinstance(prohibited, set):
                if any(item in prohibited for item in self.domain_variants(host)):
                    return False
            elif any(self.is_url_match(url, host, parsed.scheme, item) for item in prohibited):
                return False
        if is_navigation and allowed:
            if isinstance(allowed, set):
                return any(item in allowed for item in self.domain_variants(host))
            return any(self.is_url_match(url, host, parsed.scheme, item) for item in allowed)
        return True

    def report_block(self, url: str, *, resource_type: str = 'document', direct: bool = False) -> None:
        navigation = resource_type in {'document', 'sub_frame'}
        message = (
            f'Navigation blocked to disallowed URL: {url}' if direct
            else f'Navigation blocked to non-allowed URL: {url}' if navigation
            else f'Request blocked to non-allowed URL: {url}'
        )
        self.errors.append(message)
        self.event_bus.dispatch(
            BrowserErrorEvent(
                error_type='NavigationBlocked' if navigation else 'RequestBlocked',
                message=message,
                details={'url': url, 'resource_type': resource_type},
            )
        )

    async def route_request(self, route: Route) -> None:
        if getattr(self.browser_session.browser_profile, 'disable_security', False):
            await route.continue_()
            return
        request = route.request
        is_navigation = request.is_navigation_request() or request.resource_type in {'document', 'sub_frame'}
        if self.is_allowed(request.url, is_navigation=is_navigation):
            await route.continue_()
            return
        logger.warning('Blocked %s request to %s', request.resource_type, request.url)
        self.report_block(request.url, resource_type=request.resource_type)
        popup: Page | None = None
        if is_navigation:
            try:
                frame = request.frame
                candidate = frame.page
                if frame == candidate.main_frame and await candidate.opener() is not None:
                    popup = candidate
            except Exception:
                self.pending_popup_urls.append(request.url)
        try:
            await route.abort('blockedbyclient')
        except Exception:
            pass
        if popup is not None:
            await self.close_blocked_popup(popup, request.url)

    def setup_page(self, page: Page) -> None:
        async def check_navigation(frame) -> None:
            if frame != page.main_frame or not frame.url or frame.url == 'about:blank':
                return
            if self.is_allowed(frame.url, is_navigation=True):
                return
            self.report_block(frame.url, resource_type='document')
            try:
                if await page.opener() is not None:
                    await page.close()
                else:
                    await page.goto('about:blank')
            except Exception:
                pass

        page.on('framenavigated', lambda frame: asyncio.create_task(check_navigation(frame)))

    async def close_blocked_popup(self, page: Page, url: str) -> None:
        try:
            if await page.opener() is None or page.is_closed():
                return
            target_id = self.browser_session.session_manager.target_id(page)
            await page.close()
            if url in self.pending_popup_urls:
                self.pending_popup_urls.remove(url)
            self.event_bus.dispatch(TabClosedEvent(target_id=target_id))
        except Exception as exc:
            logger.debug('Failed to close blocked popup: %s', exc)

    async def close_pending_popup(self, page: Page) -> None:
        for _ in range(20):
            if page.is_closed():
                return
            if self.pending_popup_urls:
                if await page.opener() is not None:
                    await self.close_blocked_popup(page, self.pending_popup_urls[0])
                else:
                    self.pending_popup_urls.pop(0)
                return
            await asyncio.sleep(0.01)

    def reset(self) -> None:
        self.errors.clear()
        self.pending_popup_urls.clear()

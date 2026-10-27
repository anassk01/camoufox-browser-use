"""Small native equivalent of Browser-Use's watchdog registration layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from camoufox_browseruse.session import CamoufoxSession


class NativeWatchdog:
    """Own one browser concern and register its event handlers on the shared bus."""

    HANDLERS: ClassVar[dict[type[Any], str]] = {}

    def __init__(self, browser_session: 'CamoufoxSession') -> None:
        self.browser_session = browser_session

    @property
    def event_bus(self):
        return self.browser_session.event_bus

    @property
    def logger(self):
        return self.browser_session.logger

    def attach(self) -> None:
        for event_type, method_name in self.HANDLERS.items():
            self.event_bus.on(event_type, getattr(self, method_name))

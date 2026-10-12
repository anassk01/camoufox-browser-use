"""Helper utilities, keyboard classification, and boundary guards for Camoufox."""

from __future__ import annotations

import json
from typing import Any, Literal
from pydantic import BaseModel


class NoFakeCDPClient:
    """Explicit barrier when code attempts to access Chromium's CDP on Firefox/Camoufox."""

    def __bool__(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        self._raise_error(name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._raise_error('__call__')

    async def __aenter__(self) -> 'NoFakeCDPClient':
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def _raise_error(self, member: str) -> None:
        raise RuntimeError(
            f"CDP call '{member}' is not supported on Camoufox. "
            "Camoufox runs on Firefox/Gecko via Playwright and does not implement Chrome DevTools Protocol. "
            "Use native CamoufoxSession methods, Playwright page operations, or registered Camoufox tools."
        )


def tab_target_id(index: int) -> str:
    """Format target ID for a tab using a 4-digit suffix for TabInfo serializer compatibility."""
    return f"camoufox-tab-{index:04d}"


def tab_target_matches(index: int, target_id: str | None) -> bool:
    """Check if a given target_id matches the tab at index."""
    if target_id is None:
        return False
    target = str(target_id)
    return (
        target in {str(index), f"{index:04d}", tab_target_id(index), f"tab-{index:04d}", f"tab-{index}"}
        or target.endswith(f"{index:04d}")
        or target.endswith(f"-{index}")
    )


# Standard Playwright special keys that should use keyboard.press instead of keyboard.type
PLAYWRIGHT_SPECIAL_KEYS = {
    'Backquote', 'Backslash', 'Backspace', 'Delete', 'Enter', 'Escape',
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
    'Home', 'End', 'Insert', 'PageDown', 'PageUp',
    'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowUp',
    'Tab', 'Space', 'Shift', 'Control', 'Alt', 'Meta',
}

KEY_MODIFIERS = {'Control', 'Ctrl', 'Alt', 'Shift', 'Meta', 'Command'}


def classify_keyboard_input(keys: str) -> Literal['text', 'press', 'invalid']:
    """Classify keyboard input into printable text, special key press, or invalid."""
    if not keys:
        return 'invalid'

    # Single special key
    if keys in PLAYWRIGHT_SPECIAL_KEYS:
        return 'press'

    # Key chord with modifier like Control+A, Shift+Enter, etc.
    if '+' in keys:
        parts = [p.strip() for p in keys.split('+')]
        if len(parts) >= 2 and any(p.capitalize() in KEY_MODIFIERS or p.upper() == 'CTRL' for p in parts[:-1]):
            return 'press'

    # Printable string
    if all(c.isprintable() or c in '\r\n\t' for c in keys):
        return 'text'

    return 'invalid'


def stringify_value(value: Any) -> str:
    """Convert a JavaScript evaluation result or Python structure to a readable string."""
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


SENSITIVE_ATTRIBUTE_MARKERS = (
    'api-key',
    'apikey',
    'auth',
    'cookie',
    'csrf',
    'jwt',
    'key',
    'nonce',
    'password',
    'passcode',
    'secret',
    'session',
    'token',
)

MAX_SAFE_ATTRIBUTE_VALUE_LENGTH = 120
MAX_SEMANTIC_EVIDENCE_LENGTH = 240
OBSERVABLE_ELEMENT_ATTRIBUTE = 'data-browser-use-camoufox-observable'
SEMANTIC_EVIDENCE_ATTRIBUTE = 'data-browser-use-camoufox-semantic-evidence'

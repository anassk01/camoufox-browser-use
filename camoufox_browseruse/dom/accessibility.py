"""Native ARIA snapshot parsing and DOM-node enrichment for Camoufox."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


_BOX_RE = re.compile(
    r'\[box=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)\]'
)
_STATE_RE = re.compile(r'\[([A-Za-z][\w-]*)(?:=([^\]]+))?\]')
_ENTRY_RE = re.compile(r'^(?P<role>[A-Za-z][\w-]*)(?:\s+(?P<name>"(?:\\.|[^"\\])*"))?')

_INTERACTIVE_ROLES = {
    'button', 'checkbox', 'combobox', 'gridcell', 'link', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'scrollbar',
    'searchbox', 'slider', 'spinbutton', 'switch', 'tab', 'textbox', 'treeitem',
}


@dataclass(slots=True)
class AriaSnapshotNode:
    """The useful subset of one Playwright ARIA snapshot entry."""

    ax_node_id: str
    role: str
    name: str | None
    properties: dict[str, str | bool]
    box: tuple[float, float, float, float] | None
    child_ids: list[str] = field(default_factory=list)


def _scalar(value: str | None) -> str | bool:
    if value is None:
        return True
    normalized = value.strip().strip('"\'')
    if normalized.lower() == 'true':
        return True
    if normalized.lower() == 'false':
        return False
    return normalized


def _quoted_name(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return value[1:-1]


def parse_aria_snapshot(snapshot: str) -> list[AriaSnapshotNode]:
    """Parse Playwright's stable YAML-like ARIA representation without a YAML dependency."""
    parsed: list[AriaSnapshotNode] = []
    ancestry: list[tuple[int, AriaSnapshotNode]] = []
    for raw_line in snapshot.splitlines():
        indentation = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()
        if not line.startswith('- '):
            continue
        entry = line[2:]
        box_match = _BOX_RE.search(entry)
        head_match = _ENTRY_RE.match(entry)
        if not head_match:
            continue
        properties: dict[str, str | bool] = {}
        for key, value in _STATE_RE.findall(entry):
            normalized_key = {
                'haspopup': 'hasPopup',
                'readonly': 'readonly',
            }.get(key.lower(), key.lower())
            if normalized_key not in {'ref', 'box'}:
                properties[normalized_key] = _scalar(value or None)
        node = AriaSnapshotNode(
            ax_node_id=f'aria-{len(parsed) + 1}',
            role=head_match.group('role').lower(),
            name=_quoted_name(head_match.group('name')),
            properties=properties,
            box=tuple(float(value) for value in box_match.groups()) if box_match else None,
        )
        while ancestry and ancestry[-1][0] >= indentation:
            ancestry.pop()
        if ancestry:
            ancestry[-1][1].child_ids.append(node.ax_node_id)
        ancestry.append((indentation, node))
        parsed.append(node)
    return parsed


def _implicit_role(element: dict[str, Any]) -> str | None:
    attrs = element.get('attributes') or {}
    explicit = str(attrs.get('role') or '').strip().lower()
    if explicit:
        return explicit
    tag = str(element.get('tagName') or '').lower()
    input_type = str(attrs.get('type') or 'text').lower()
    if tag == 'a' and attrs.get('href') is not None:
        return 'link'
    if tag in {'button', 'summary'}:
        return 'button'
    if tag == 'textarea':
        return 'textbox'
    if tag == 'select':
        return 'listbox' if 'multiple' in attrs else 'combobox'
    if tag == 'option':
        return 'option'
    if tag == 'input':
        return {
            'button': 'button', 'submit': 'button', 'reset': 'button', 'image': 'button',
            'checkbox': 'checkbox', 'radio': 'radio', 'range': 'slider',
            'number': 'spinbutton', 'search': 'searchbox',
        }.get(input_type, None if input_type == 'hidden' else 'textbox')
    return {
        'h1': 'heading', 'h2': 'heading', 'h3': 'heading', 'h4': 'heading',
        'h5': 'heading', 'h6': 'heading', 'img': 'img', 'main': 'main',
        'nav': 'navigation', 'article': 'article', 'ul': 'list', 'ol': 'list',
        'li': 'listitem', 'table': 'table', 'th': 'columnheader', 'progress': 'progressbar',
        'output': 'status',
    }.get(tag)


def _fallback_name(element: dict[str, Any]) -> str | None:
    attrs = element.get('attributes') or {}
    for key in (
        'aria-label', 'data-browser-use-camoufox-labels', 'alt', 'title', 'placeholder',
    ):
        value = str(attrs.get(key) or '').strip()
        if value:
            return value
    text = str(element.get('text') or '').strip()
    return text or None


def _dom_properties(element: dict[str, Any]) -> dict[str, str | bool]:
    attrs = element.get('attributes') or {}
    properties: dict[str, str | bool] = {}
    aria_names = {
        'aria-checked': 'checked', 'aria-selected': 'selected',
        'aria-expanded': 'expanded', 'aria-pressed': 'pressed',
        'aria-disabled': 'disabled', 'aria-invalid': 'invalid',
        'aria-required': 'required', 'aria-busy': 'busy', 'aria-live': 'live',
        'aria-current': 'current', 'aria-orientation': 'orientation',
        'aria-sort': 'sort', 'aria-level': 'level',
        'aria-valuemin': 'valuemin', 'aria-valuemax': 'valuemax',
        'aria-valuenow': 'valuenow', 'aria-valuetext': 'valuetext',
        'aria-keyshortcuts': 'keyshortcuts', 'aria-haspopup': 'hasPopup',
        'aria-multiselectable': 'multiselectable', 'aria-readonly': 'readonly',
    }
    for attribute, property_name in aria_names.items():
        if attribute in attrs:
            properties[property_name] = _scalar(str(attrs[attribute]))
    for attribute in ('checked', 'selected', 'disabled', 'required', 'readonly'):
        if attribute in attrs:
            properties[attribute] = _scalar(str(attrs[attribute]) or None)
    tag = str(element.get('tagName') or '').lower()
    if tag.startswith('h') and len(tag) == 2 and tag[1].isdigit():
        properties.setdefault('level', tag[1])
    return properties


def _match_score(element: dict[str, Any], node: AriaSnapshotNode) -> float | None:
    if node.box is None:
        return None
    rect = element.get('rect') or {}
    box = (
        float(rect.get('x', 0)), float(rect.get('y', 0)),
        float(rect.get('width', 0)), float(rect.get('height', 0)),
    )
    deltas = [abs(left - right) for left, right in zip(box, node.box, strict=True)]
    if max(deltas) > 3.0:
        return None
    score = 40.0 - sum(deltas)
    expected_role = _implicit_role(element)
    if expected_role == node.role:
        score += 100.0
    elif expected_role:
        score -= 30.0
    expected_name = (_fallback_name(element) or '').casefold()
    if node.name and expected_name:
        native_name = node.name.casefold()
        if native_name == expected_name:
            score += 30.0
        elif native_name in expected_name or expected_name in native_name:
            score += 10.0
    return score


def merge_accessibility(elements: list[dict[str, Any]], snapshot: str) -> None:
    """Join native ARIA semantics onto selected DOM payloads in-place."""
    native_nodes = parse_aria_snapshot(snapshot)
    matches: dict[int, AriaSnapshotNode] = {}
    unused_elements = set(range(len(elements)))

    # Assign each native AX entry to at most one DOM element. Exact role and
    # box agreement wins when wrappers and controls share the same rectangle.
    for native in native_nodes:
        candidates = [
            (score, index)
            for index in unused_elements
            if (score := _match_score(elements[index], native)) is not None
        ]
        if not candidates:
            continue
        _score, index = max(candidates)
        matches[index] = native
        unused_elements.remove(index)

    for index, element in enumerate(elements):
        native = matches.get(index)
        role = native.role if native else _implicit_role(element)
        name = native.name if native and native.name else _fallback_name(element)
        properties = dict(native.properties) if native else {}
        properties.update(_dom_properties(element))
        element['accessibility'] = {
            'ax_node_id': native.ax_node_id if native else None,
            'role': role,
            'name': name,
            'description': str((element.get('attributes') or {}).get('aria-description') or '').strip() or None,
            'properties': properties,
            'child_ids': native.child_ids if native else None,
            'source': 'native-aria' if native else 'dom-fallback',
        }
        if role in _INTERACTIVE_ROLES and not element.get('is_disabled', False):
            element['is_interactive'] = True

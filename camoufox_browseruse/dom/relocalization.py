"""Stale element relocalization to match nodes across DOM mutations."""

from __future__ import annotations

from typing import Any
from browser_use.dom.views import EnhancedDOMTreeNode


MIN_RELOCALIZATION_SCORE = 25
AMBIGUITY_MARGIN = 10


def _has_semantic_match(target: EnhancedDOMTreeNode, candidate: EnhancedDOMTreeNode) -> bool:
    """Require evidence that survives extraction; geometry and transient indexes are not identity."""
    t_attrs = target.attributes or {}
    c_attrs = candidate.attributes or {}

    # A strong identity captured with the original observation must still match.
    # Falling back to text when it changes could hit a different control with
    # the same label.
    for key in ('id', 'data-testid', 'name'):
        if t_attrs.get(key) and t_attrs.get(key) != c_attrs.get(key):
            return False

    for key in ('id', 'data-testid', 'name'):
        if t_attrs.get(key) and t_attrs.get(key) == c_attrs.get(key):
            return True

    t_classes = set((t_attrs.get('class') or '').split())
    c_classes = set((c_attrs.get('class') or '').split())
    if t_classes and t_classes & c_classes:
        return True

    t_text = (target.node_value or '').strip().lower()
    c_text = (candidate.node_value or '').strip().lower()
    return bool(t_text and c_text and (t_text == c_text or t_text in c_text or c_text in t_text))


def action_node_match_score(target: EnhancedDOMTreeNode, candidate: EnhancedDOMTreeNode) -> int:
    """Calculate matching score between a target node and a candidate node."""
    if target.node_name.lower() != candidate.node_name.lower():
        return 0

    score = 0

    t_attrs = target.attributes or {}
    c_attrs = candidate.attributes or {}

    # Exact ID match
    if t_attrs.get('id') and t_attrs.get('id') == c_attrs.get('id'):
        score += 50

    # data-testid match
    if t_attrs.get('data-testid') and t_attrs.get('data-testid') == c_attrs.get('data-testid'):
        score += 50

    # name match
    if t_attrs.get('name') and t_attrs.get('name') == c_attrs.get('name'):
        score += 30

    # role and type match
    if t_attrs.get('role') and t_attrs.get('role') == c_attrs.get('role'):
        score += 20
    if t_attrs.get('type') and t_attrs.get('type') == c_attrs.get('type'):
        score += 20

    # class match
    t_class = (t_attrs.get('class') or '').strip()
    c_class = (c_attrs.get('class') or '').strip()
    if t_class and t_class == c_class:
        score += 25
    elif t_class and c_class:
        t_classes = set(t_class.split())
        c_classes = set(c_class.split())
        if t_classes & c_classes:
            score += 15

    # text content match
    t_text = (target.node_value or '').strip().lower()
    c_text = (candidate.node_value or '').strip().lower()
    if t_text and c_text:
        if t_text == c_text:
            score += 30
        elif t_text in c_text or c_text in t_text:
            score += 15

    # Positional proximity
    if target.absolute_position and candidate.absolute_position:
        dx = abs(target.absolute_position.x - candidate.absolute_position.x)
        dy = abs(target.absolute_position.y - candidate.absolute_position.y)
        if dx < 10 and dy < 10:
            score += 25
        elif dx < 50 and dy < 50:
            score += 10

    return score


def find_best_matching_node(
    target_node: EnhancedDOMTreeNode,
    candidates: list[EnhancedDOMTreeNode],
    min_score: int = MIN_RELOCALIZATION_SCORE,
    ambiguity_margin: int = AMBIGUITY_MARGIN,
) -> EnhancedDOMTreeNode | None:
    """Find the best matching candidate node for a target node from fresh candidates."""
    scored: list[tuple[EnhancedDOMTreeNode, int]] = []
    for candidate in candidates:
        if not candidate.is_visible or not _has_semantic_match(target_node, candidate):
            continue
        score = action_node_match_score(target_node, candidate)
        if score >= min_score:
            scored.append((candidate, score))

    if not scored:
        return None

    scored.sort(key=lambda item: -item[1])
    best_candidate, best_score = scored[0]

    if len(scored) == 1:
        return best_candidate

    second_score = scored[1][1]

    # Check for strong unique identity on candidate 0
    t_attrs = target_node.attributes or {}
    c_attrs = best_candidate.attributes or {}
    has_unique_id = any(
        t_attrs.get(key) and t_attrs.get(key) == c_attrs.get(key)
        and sum((node.attributes or {}).get(key) == t_attrs[key] for node, _ in scored) == 1
        for key in ('id', 'data-testid')
    )

    if has_unique_id:
        return best_candidate

    # If the score difference is within ambiguity margin, reject ambiguous match
    if (best_score - second_score) < ambiguity_margin:
        return None

    return best_candidate

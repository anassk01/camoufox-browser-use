"""DOM serialization into Browser-Use compatible EnhancedDOMTreeNode and SerializedDOMState."""

from __future__ import annotations

from typing import Any, Literal, cast
from playwright.async_api import Page

from browser_use.browser.views import PageInfo
from browser_use.dom.views import (
    DEFAULT_INCLUDE_ATTRIBUTES,
    DOMRect,
    EnhancedAXNode,
    EnhancedAXProperty,
    EnhancedDOMTreeNode,
    EnhancedSnapshotNode,
    NodeType,
    SerializedDOMState,
    SimplifiedNode,
)
from camoufox_browseruse.helpers import OBSERVABLE_ELEMENT_ATTRIBUTE, SEMANTIC_EVIDENCE_ATTRIBUTE


class CamoufoxSerializedDOMState(SerializedDOMState):
    """Browser-Use DOM state with native relationship evidence exposed to the model."""

    @staticmethod
    def _attributes(include_attributes: list[str] | None) -> list[str]:
        attributes = list(include_attributes or DEFAULT_INCLUDE_ATTRIBUTES)
        if SEMANTIC_EVIDENCE_ATTRIBUTE not in attributes:
            attributes.append(SEMANTIC_EVIDENCE_ATTRIBUTE)
        return attributes

    def llm_representation(self, include_attributes: list[str] | None = None) -> str:
        return super().llm_representation(self._attributes(include_attributes))

    def eval_representation(self, include_attributes: list[str] | None = None) -> str:
        return super().eval_representation(self._attributes(include_attributes))


def node_from_payload(
    payload: dict[str, Any],
    node_id: int,
    attributes: dict[str, str],
    *,
    backend_node_id: int | None = None,
) -> EnhancedDOMTreeNode:
    """Construct an EnhancedDOMTreeNode from extracted payload dictionary."""
    rect = payload.get('rect', {'x': 0, 'y': 0, 'width': 0, 'height': 0})
    bounds = DOMRect(
        x=float(rect.get('x', 0)),
        y=float(rect.get('y', 0)),
        width=float(rect.get('width', 0)),
        height=float(rect.get('height', 0)),
    )
    shadow_root_type = cast(Literal['user-agent', 'open', 'closed'] | None, payload.get('shadow_root_type') or None)

    is_interactive = bool(payload.get('is_interactive', True))
    is_disabled = bool(payload.get('is_disabled', False))
    accessibility = payload.get('accessibility') or {}
    ax_properties = [
        EnhancedAXProperty(name=property_name, value=value)
        for property_name, value in (accessibility.get('properties') or {}).items()
    ]
    ax_node = EnhancedAXNode(
        ax_node_id=str(accessibility.get('ax_node_id') or f"camoufox-ax:{payload.get('tree_id', node_id)}"),
        ignored=False,
        role=accessibility.get('role'),
        name=accessibility.get('name'),
        description=accessibility.get('description'),
        properties=ax_properties or None,
        child_ids=accessibility.get('child_ids') or None,
    ) if accessibility else None

    text_val = str(payload.get('text') or '').strip()
    children_nodes = None
    if text_val:
        txt_node = EnhancedDOMTreeNode(
            node_id=node_id * 1000,
            backend_node_id=node_id * 1000,
            node_type=NodeType.TEXT_NODE,
            node_name='#text',
            node_value=text_val,
            attributes={},
            is_scrollable=None,
            is_visible=True,
            absolute_position=bounds,
            target_id='camoufox',
            frame_id=str(payload.get('frame_id') or 'main'),
            session_id=None,
            content_document=None,
            shadow_root_type=None,
            shadow_roots=None,
            parent_node=None,
            children_nodes=None,
            ax_node=None,
            snapshot_node=EnhancedSnapshotNode(
                is_clickable=False,
                cursor_style=None,
                bounds=bounds,
                clientRects=bounds,
                scrollRects=None,
                computed_styles=None,
                paint_order=None,
                stacking_contexts=None,
            ),
        )
        children_nodes = [txt_node]

    node = EnhancedDOMTreeNode(
        node_id=node_id,
        backend_node_id=node_id if backend_node_id is None else backend_node_id,
        node_type=NodeType.ELEMENT_NODE,
        node_name=str(payload.get('tagName', 'DIV')),
        node_value=str(payload.get('text') or ''),
        attributes=attributes,
        is_scrollable=None,
        is_visible=bool(payload.get('is_visible', True)),
        absolute_position=bounds,
        target_id='camoufox',
        frame_id=str(payload.get('frame_id') or 'main'),
        session_id=None,
        content_document=None,
        shadow_root_type=shadow_root_type,
        shadow_roots=None,
        parent_node=None,
        children_nodes=children_nodes,
        ax_node=ax_node,
        snapshot_node=EnhancedSnapshotNode(
            is_clickable=is_interactive and not is_disabled,
            cursor_style=None,
            bounds=bounds,
            clientRects=bounds,
            scrollRects=None,
            computed_styles=None,
            paint_order=None,
            stacking_contexts=None,
        ),
    )
    if children_nodes:
        children_nodes[0].parent_node = node
    return node


def text_node_for(parent: EnhancedDOMTreeNode) -> EnhancedDOMTreeNode:
    """Create a virtual text child node so DOMTreeSerializer renders inner text."""
    return EnhancedDOMTreeNode(
        node_id=parent.node_id * 1000,
        backend_node_id=parent.backend_node_id * 1000,
        node_type=NodeType.TEXT_NODE,
        node_name='#text',
        node_value=parent.node_value,
        attributes={},
        is_scrollable=None,
        is_visible=True,
        absolute_position=parent.absolute_position,
        target_id=parent.target_id,
        frame_id=parent.frame_id,
        session_id=parent.session_id,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=parent,
        children_nodes=None,
        ax_node=None,
        snapshot_node=parent.snapshot_node,
    )


def build_simplified_tree(
    nodes: list[tuple[EnhancedDOMTreeNode, bool, str, str | None, bool]],
) -> SimplifiedNode | None:
    """Build the selected DOM hierarchy expected by DOMTreeSerializer."""
    if not nodes:
        return None

    root_node = EnhancedDOMTreeNode(
        node_id=0,
        backend_node_id=0,
        node_type=NodeType.DOCUMENT_NODE,
        node_name='#document',
        node_value='',
        attributes={},
        is_scrollable=None,
        is_visible=True,
        absolute_position=None,
        target_id='camoufox',
        frame_id='main',
        session_id=None,
        content_document=None,
        shadow_root_type=None,
        shadow_roots=None,
        parent_node=None,
        children_nodes=[],
        ax_node=None,
        snapshot_node=None,
    )

    root = SimplifiedNode(original_node=root_node, children=[])
    simplified_by_tree_id: dict[str, SimplifiedNode] = {}
    parent_ids: dict[str, str | None] = {}
    for node, is_interactive, tree_id, parent_tree_id, is_shadow_host in nodes:
        simplified_by_tree_id[tree_id] = SimplifiedNode(
            original_node=node,
            children=[],
            is_interactive=is_interactive,
            # Browser-Use >=0.13 renders the model-visible selector index, which
            # here is the selector-map key used as backend_node_id.
            selector_index=node.backend_node_id if is_interactive else None,
            is_shadow_host=is_shadow_host,
        )
        parent_ids[tree_id] = parent_tree_id

    for node, _is_interactive, tree_id, _parent_tree_id, _is_shadow_host in nodes:
        simplified = simplified_by_tree_id[tree_id]
        parent = simplified_by_tree_id.get(parent_ids[tree_id] or '')
        if parent is None:
            node.parent_node = root_node
            root.children.append(simplified)
        else:
            node.parent_node = parent.original_node
            parent.children.append(simplified)

    def materialize_children(simplified: SimplifiedNode) -> None:
        node = simplified.original_node
        for child in simplified.children:
            materialize_children(child)

        element_children = [child.original_node for child in simplified.children]
        # innerText on a semantic container includes all descendant text. Only
        # emit it for leaves or controls; otherwise descendants are the source
        # of truth and repeating the aggregate bloats the model observation.
        text_children: list[SimplifiedNode] = []
        text_nodes: list[EnhancedDOMTreeNode] = []
        child_text = ' '.join(
            child.node_value.strip()
            for child in element_children
            if child.node_value and child.node_value.strip()
        )
        repeats_selected_children = child_text and child_text.casefold() == node.node_value.strip().casefold()
        if (
            node.node_value
            and node.node_value.strip()
            and (simplified.is_interactive or not element_children)
            and not repeats_selected_children
        ):
            text_node = text_node_for(node)
            text_children.append(SimplifiedNode(original_node=text_node, children=[]))
            text_nodes.append(text_node)
        simplified.children[:0] = text_children
        node.children_nodes = text_nodes + element_children or None

    for child in root.children:
        materialize_children(child)
    root_node.children_nodes = [child.original_node for child in root.children]

    return root


def serialize_dom_state(
    elements: list[dict[str, Any]],
    selector_targets: dict[int, dict[str, Any]],
) -> tuple[CamoufoxSerializedDOMState, dict[int, EnhancedDOMTreeNode]]:
    """Convert extracted element payloads into SerializedDOMState and selector map."""
    selector_map: dict[int, EnhancedDOMTreeNode] = {}
    tree_nodes: list[tuple[EnhancedDOMTreeNode, bool, str, str | None, bool]] = []
    selector_targets.clear()

    for tree_position, element in enumerate(elements, start=1):
        is_interactive = bool(element.get('is_interactive', True))
        index = len(selector_map) + 1 if is_interactive else None
        selector = str(element.get('selector') or element.get('tagName') or '*')
        ordinal = int(element.get('ordinal', 0))
        target = {
            'selector': selector,
            'ordinal': ordinal,
            'frame_id': element.get('frame_id', 'main'),
            'frame_url': element.get('frame_url', ''),
            'shadow_root_type': element.get('shadow_root_type'),
            'frame_depth': int(element.get('frame_depth', 0)),
        }
        attributes = dict(element.get('attributes') or {})
        accessibility = element.get('accessibility') or {}
        accessible_name = str(accessibility.get('name') or '').strip()
        if accessible_name and accessible_name.casefold() != str(element.get('text') or '').strip().casefold():
            attributes['ax_name'] = accessible_name
        if accessibility.get('source'):
            attributes['data-browser-use-camoufox-ax-source'] = str(accessibility['source'])
        attributes['data-browser-use-camoufox-selector'] = selector
        attributes['data-browser-use-camoufox-ordinal'] = str(ordinal)
        attributes['data-browser-use-camoufox-disabled'] = str(bool(element.get('is_disabled', False))).lower()
        attributes['data-browser-use-camoufox-frame'] = str(target['frame_id'])
        attributes['data-browser-use-camoufox-frame-depth'] = str(target['frame_depth'])
        if element.get('child_frame_id'):
            attributes['data-browser-use-camoufox-child-frame'] = str(element['child_frame_id'])

        if not is_interactive:
            attributes[OBSERVABLE_ELEMENT_ATTRIBUTE] = 'true'

        # Browser-Use keeps static text in the serialized observation tree but
        # reserves selector-map IDs for actionable controls.  Static nodes use
        # negative internal IDs so they can never collide with action indices.
        node_id = index if index is not None else -tree_position
        node = node_from_payload(
            element,
            node_id=node_id,
            attributes=attributes,
            backend_node_id=node_id,
        )
        tree_id = str(element.get('tree_id') or f'flat:{tree_position}')
        parent_tree_id = element.get('parent_tree_id')
        tree_nodes.append((
            node,
            is_interactive,
            tree_id,
            str(parent_tree_id) if parent_tree_id is not None else None,
            bool(element.get('is_shadow_host', False)),
        ))
        if index is not None:
            selector_targets[index] = target
            selector_map[index] = node

    root = build_simplified_tree(tree_nodes)
    return CamoufoxSerializedDOMState(_root=root, selector_map=selector_map), selector_map


async def extract_page_info(page: Page) -> PageInfo:
    """Calculate PageInfo for the current page state."""
    info = await page.evaluate(
        """() => {
            const doc = document.documentElement || document.body;
            const vw = window.innerWidth || doc.clientWidth || 1280;
            const vh = window.innerHeight || doc.clientHeight || 720;
            const pw = Math.max(doc.scrollWidth || 0, doc.offsetWidth || 0, doc.clientWidth || 0, vw);
            const ph = Math.max(doc.scrollHeight || 0, doc.offsetHeight || 0, doc.clientHeight || 0, vh);
            const sx = window.scrollX || window.pageXOffset || 0;
            const sy = window.scrollY || window.pageYOffset || 0;
            return {
                viewport_width: Math.round(vw),
                viewport_height: Math.round(vh),
                page_width: Math.round(pw),
                page_height: Math.round(ph),
                scroll_x: Math.round(sx),
                scroll_y: Math.round(sy),
                pixels_above: Math.max(0, Math.round(sy)),
                pixels_below: Math.max(0, Math.round(ph - sy - vh)),
                pixels_left: Math.max(0, Math.round(sx)),
                pixels_right: Math.max(0, Math.round(pw - sx - vw)),
            };
        }"""
    )
    return PageInfo(**info)

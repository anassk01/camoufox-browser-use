"""DOM extraction and serialization for Camoufox."""

from camoufox_browseruse.dom.accessibility import merge_accessibility, parse_aria_snapshot
from camoufox_browseruse.dom.extractor import (
    FrameExtractionOptions,
    FrameRegistry,
    extract_interactive_elements,
)
from camoufox_browseruse.dom.serializer import extract_page_info, serialize_dom_state
from camoufox_browseruse.dom.relocalization import find_best_matching_node

__all__ = [
    'extract_interactive_elements',
    'FrameExtractionOptions',
    'FrameRegistry',
    'serialize_dom_state',
    'extract_page_info',
    'find_best_matching_node',
    'merge_accessibility',
    'parse_aria_snapshot',
]

"""Narrow compatibility hooks for Browser-Use's MCP server."""

from __future__ import annotations

import json
import time
from functools import wraps

_APPLIED = False


def apply_camoufox_mcp_compat() -> None:
    global _APPLIED
    if _APPLIED:
        return
    try:
        from browser_use.mcp.server import BrowserUseServer
    except ImportError:
        return
    from camoufox_browseruse.session import CamoufoxSession

    original_get_html = BrowserUseServer._get_html
    original_list_sessions = BrowserUseServer._list_sessions

    @wraps(original_get_html)
    async def get_html(server, selector: str | None = None):
        if isinstance(server.browser_session, CamoufoxSession):
            server._update_session_activity(server.browser_session.id)
            return await server.browser_session.get_html(selector)
        return await original_get_html(server, selector)

    @wraps(original_list_sessions)
    async def list_sessions(server):
        if not server.active_sessions:
            return 'No active browser sessions'
        if not any(isinstance(data.get('session'), CamoufoxSession) for data in server.active_sessions.values()):
            return await original_list_sessions(server)
        sessions = []
        for session_id, data in server.active_sessions.items():
            session = data['session']
            sessions.append({
                'session_id': session_id,
                'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['created_at'])),
                'last_activity': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['last_activity'])),
                'active': await session.is_active() if isinstance(session, CamoufoxSession) else True,
                'current_url': await session.get_current_page_url() if isinstance(session, CamoufoxSession) else data.get('url', 'Unknown'),
                'age_minutes': (time.time() - data['created_at']) / 60,
            })
        return json.dumps(sessions, indent=2)

    BrowserUseServer._get_html = get_html
    BrowserUseServer._list_sessions = list_sessions
    _APPLIED = True

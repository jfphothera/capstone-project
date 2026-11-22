"""Opaque, expiring developer and read-only viewer sessions."""
from __future__ import annotations

import hmac
import secrets
import time
import os
import hashlib
from fastapi import Header, HTTPException, Request


COOKIE = 'capstone_developer_session'
SESSION_SECONDS = 12 * 60 * 60
VIEWER_SESSION_SECONDS = 7 * 24 * 60 * 60


def _configured(request: Request):
    return getattr(request.app.state, 'developer_token', None)


def valid_configured_token(request: Request, token: str | None) -> bool:
    configured = _configured(request)
    return bool(configured and token and
                hmac.compare_digest(str(configured), str(token)))


def create_session(request: Request, token: str, *, role='viewer'):
    viewer=os.environ.get('CAPSTONE_VIEWER_TOKEN')
    scoped_viewer=bool(role=='viewer' and viewer and token and hmac.compare_digest(viewer,token))
    if role not in {'viewer', 'developer'} or not (scoped_viewer or valid_configured_token(request, token)):
        raise HTTPException(403, 'Developer authorization required')
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    request.app.state.developer_sessions[session_id] = {
        'role': role, 'expires_at': time.time() + (VIEWER_SESSION_SECONDS if role=='viewer' else SESSION_SECONDS), 'csrf': csrf}
    if role=='viewer':
        from .runtime import get_runtime
        get_runtime().store.upsert('arena_viewer_sessions',{'session_hash':hashlib.sha256(session_id.encode()).hexdigest(),
            'expires_at':request.app.state.developer_sessions[session_id]['expires_at'],'csrf':csrf},'session_hash')
    return session_id, csrf


def _session(request: Request):
    session_id = request.cookies.get(COOKIE) or ''
    row = request.app.state.developer_sessions.get(session_id)
    if not row and session_id:
        from .runtime import get_runtime
        saved=get_runtime().store.one('SELECT * FROM arena_viewer_sessions WHERE session_hash=?',
            (hashlib.sha256(session_id.encode()).hexdigest(),))
        if saved:row={'role':'viewer','expires_at':saved['expires_at'],'csrf':saved['csrf']}
    if not row:
        return None
    if row['expires_at'] <= time.time():
        request.app.state.developer_sessions.pop(session_id, None)
        return None
    return row


def renew_developer_session(request: Request):
    """Keep an actively open Testing Lab session alive without exposing its credential."""
    row = _session(request)
    if row and row.get('role') == 'developer':
        row['expires_at'] = time.time() + SESSION_SECONDS
    return row


def authorized(request: Request, header: str | None = None, *, viewer_ok=False) -> bool:
    if getattr(request.app.state, 'developer_test_bypass', False) and not _configured(request):
        return True
    if valid_configured_token(request, header):
        return True
    row = _session(request)
    return bool(row and (row['role'] == 'developer' or viewer_ok and row['role'] == 'viewer'))


async def require_developer(request: Request,
                      x_capstone_developer_token: str | None = Header(default=None)):
    if not authorized(request, x_capstone_developer_token):
        raise HTTPException(403, 'Developer authorization required')
    return True


async def require_viewer(request: Request,
                   x_capstone_developer_token: str | None = Header(default=None)):
    if not authorized(request, x_capstone_developer_token, viewer_ok=True):
        raise HTTPException(403, 'Viewer authorization required')
    return True


def require_csrf(request: Request, x_csrf_token: str | None = Header(default=None)):
    row = _session(request)
    if not row or not x_csrf_token or not hmac.compare_digest(row['csrf'], x_csrf_token):
        raise HTTPException(403, 'CSRF validation failed')
    return True

"""BFF-issued session tokens.

Apple identity tokens are short-lived and meant for one-time verification,
not to be resent on every API call -- so /me issues its own signed session
token (a plain HMAC JWT) once, which the iOS app stores and sends as the
Bearer token for every subsequent call instead. This token only ever proves
"the BFF vouches this is user_id X" -- it carries no Databricks/Apple
credentials of its own.
"""
from __future__ import annotations

import time

import jwt

from .config import settings

ALGORITHM = "HS256"
TTL_SECONDS = 30 * 24 * 3600  # 30 days -- refreshed by re-signing in with Apple


class SessionError(Exception):
    pass


def issue(user_id: str) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + TTL_SECONDS}
    return jwt.encode(payload, settings.session_secret, algorithm=ALGORITHM)


def verify(token: str) -> str:
    """Returns the user_id if valid, raises SessionError otherwise."""
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise SessionError(str(exc)) from exc
    user_id = payload.get("sub")
    if not user_id:
        raise SessionError("session token missing sub")
    return user_id

"""Per-request values used for safe correlated logging."""
from __future__ import annotations

from contextvars import ContextVar, Token

request_id: ContextVar[str] = ContextVar("request_id", default="-")
user_id: ContextVar[str] = ContextVar("user_id", default="-")
identity_issuer: ContextVar[str] = ContextVar("identity_issuer", default="-")


def begin_request(value: str) -> tuple[Token[str], Token[str], Token[str]]:
    return (
        request_id.set(value),
        user_id.set("-"),
        identity_issuer.set("-"),
    )


def end_request(tokens: tuple[Token[str], Token[str], Token[str]]) -> None:
    request_id.reset(tokens[0])
    user_id.reset(tokens[1])
    identity_issuer.reset(tokens[2])

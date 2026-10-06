"""Verify Sign in with Apple identity tokens.

The iOS app sends the raw identity token it gets from AuthenticationServices
(ASAuthorizationAppleIDCredential.identityToken) as a Bearer token. This
module verifies it came from Apple, wasn't tampered with, and hasn't
expired -- nothing more. It does not know about the users table; that
resolution happens one layer up (see main.py).

Apple's public keys rotate, so they're fetched from Apple's JWKS endpoint
and cached rather than hardcoded. See:
https://developer.apple.com/documentation/sign_in_with_apple/verifying_a_user
"""
from __future__ import annotations

import time

import httpx
import jwt
from jwt import PyJWKClient

from .config import settings

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

# PyJWKClient handles fetching + caching Apple's public keys internally
# (keyed by `kid`), including re-fetching on a cache miss for key rotation.
_jwks_client = PyJWKClient(APPLE_JWKS_URL, cache_keys=True, lifespan=3600)


class AppleTokenError(Exception):
    """Raised when an identity token fails verification for any reason."""


def verify_identity_token(token: str) -> dict:
    """Verify an Apple identity token and return its claims.

    Returns a dict with at least `sub` (Apple's stable per-user id -- stored
    as apple_sub on the users row), and usually `email`/`is_private_email`
    on first sign-in (Apple omits email on subsequent sign-ins by design).

    Raises AppleTokenError on any verification failure -- bad signature,
    wrong issuer/audience, expired token, malformed token.
    """
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.apple_bundle_id,
            issuer=APPLE_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AppleTokenError(f"invalid Apple identity token: {exc}") from exc

    if not claims.get("sub"):
        raise AppleTokenError("Apple identity token missing sub claim")
    return claims

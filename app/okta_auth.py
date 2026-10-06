"""Verify Okta CIAM (Auth0) or Workforce OIDC identity tokens."""
from __future__ import annotations

import hmac
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient

from .config import settings

_jwks_client: PyJWKClient | None = None
_jwks_client_url: str | None = None


class OktaTokenError(Exception):
    """Raised when an Okta identity token fails verification."""


def issuer_candidates(configured: str) -> list[str]:
    """Auth0 `iss` usually has a trailing slash; Workforce often does not."""
    base = configured.rstrip("/")
    if not base:
        return []
    return [base, f"{base}/"]


def issuers_match(token_iss: object, configured: str) -> bool:
    return isinstance(token_iss, str) and bool(configured) and token_iss.rstrip("/") == configured.rstrip("/")


def default_jwks_url(issuer: str, explicit: str = "") -> str:
    """Auth0 JWKS is `/.well-known/jwks.json`; Workforce default AS is `/v1/keys`."""
    if explicit:
        return explicit
    host = (urlparse(issuer).hostname or "").lower()
    base = issuer.rstrip("/")
    if host.endswith(".auth0.com"):
        return f"{base}/.well-known/jwks.json"
    return f"{base}/v1/keys"


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client, _jwks_client_url

    if not settings.okta_issuer or not settings.okta_audience:
        raise OktaTokenError("Okta identity provider is not configured")
    if not settings.okta_issuer.startswith("https://"):
        raise OktaTokenError("Okta issuer must use HTTPS")

    jwks_url = default_jwks_url(settings.okta_issuer, settings.okta_jwks_url)
    if not jwks_url.startswith("https://"):
        raise OktaTokenError("Okta JWKS URL must use HTTPS")
    if _jwks_client is None or _jwks_client_url != jwks_url:
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        _jwks_client_url = jwks_url
    return _jwks_client


def verify_identity_token(token: str, *, expected_nonce: str | None = None) -> dict:
    """Verify signature, issuer, audience, lifetime, and stable subject."""
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.okta_audience,
            issuer=issuer_candidates(settings.okta_issuer),
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except OktaTokenError:
        raise
    except jwt.PyJWTError as exc:
        raise OktaTokenError(f"invalid Okta identity token: {exc}") from exc

    if not claims.get("sub"):
        raise OktaTokenError("Okta identity token missing sub claim")
    # Nonce check is keyed on the TOKEN's own claim, not on whether the
    # caller supplied one: Auth0 echoes the original /authorize nonce into
    # an ID token minted via the interactive authorization-code flow, so
    # its presence there means "verify it" -- but a refreshed ID token
    # (grant_type=refresh_token, the silent Face-ID-gated relogin path)
    # never carries a nonce at all, nothing to replay-protect against,
    # since possession of the refresh token itself (locked behind
    # biometryCurrentSet Keychain access control on the device) is the
    # trust boundary for that leg instead. A present nonce with no
    # expected value to check it against is always rejected outright.
    token_nonce = claims.get("nonce")
    if token_nonce is not None:
        if not isinstance(expected_nonce, str) or not hmac.compare_digest(token_nonce, expected_nonce):
            raise OktaTokenError("Okta identity token nonce does not match")
    return claims

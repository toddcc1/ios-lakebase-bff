"""Route end-user identity tokens to an explicitly configured verifier."""
from __future__ import annotations

from dataclasses import dataclass

import jwt

from . import apple_auth, okta_auth
from .config import settings


class IdentityTokenError(Exception):
    """Raised when an identity token cannot be safely routed or verified."""


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None

    @property
    def resolver_payload(self) -> dict:
        return {
            f"{self.provider}_sub": self.subject,
            "email": self.email,
            "email_verified": self.email_verified,
            "display_name": self.display_name,
        }


def _claim_is_true(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def verify_identity_token(
    token: str,
    *,
    expected_oidc_nonce: str | None = None,
) -> VerifiedIdentity:
    allowed = settings.auth_issuer
    if allowed not in {"apple", "okta", "both"}:
        raise IdentityTokenError("BFF AUTH_ISSUER must be apple, okta, or both")

    try:
        unverified = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise IdentityTokenError("malformed identity token") from exc

    issuer = unverified.get("iss")
    try:
        if issuer == apple_auth.APPLE_ISSUER and allowed in {"apple", "both"}:
            claims = apple_auth.verify_identity_token(token)
            provider = "apple"
        elif (
            settings.okta_issuer
            and okta_auth.issuers_match(issuer, settings.okta_issuer)
            and allowed in {"okta", "both"}
        ):
            # No blanket "nonce required" gate here -- okta_auth.py decides
            # per-token: an interactively-issued ID token (fresh PKCE login)
            # always carries a nonce and must match; a refreshed ID token
            # (grant_type=refresh_token, the Face-ID-gated silent relogin
            # path) never carries one, and that's expected, not an error.
            claims = okta_auth.verify_identity_token(
                token,
                expected_nonce=expected_oidc_nonce,
            )
            provider = "okta"
        else:
            raise IdentityTokenError("identity token issuer is not allowed")
    except (apple_auth.AppleTokenError, okta_auth.OktaTokenError) as exc:
        raise IdentityTokenError(str(exc)) from exc

    return VerifiedIdentity(
        provider=provider,
        subject=claims["sub"],
        email=claims.get("email"),
        email_verified=_claim_is_true(claims.get("email_verified")),
        display_name=claims.get("name"),
    )

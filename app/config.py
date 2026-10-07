"""Environment-driven settings -- no workspace/account literals in source.

Every identifier that differs between environments comes from an env var, so
this service is portable across hosts and Databricks workspaces by config
change only.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Local dev only: reads .env into the process environment. In Azure Container
# Apps, real env vars/secrets are already set on the container -- this is a
# harmless no-op there (no .env file present).
load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Accepted end-user identity issuers: apple, okta, or both.
    auth_issuer: str

    # Sign in with Apple: your app's bundle id (native) is the `aud` claim
    # on the identity token. A Services ID would be used instead for web.
    apple_bundle_id: str

    # Okta CIAM (Auth0 developer tenant) or Workforce authorization server.
    # Issuer / audience / JWKS are env-only so the IdP can be swapped without
    # a code change. Audience is the public native client id (never a secret).
    okta_issuer: str
    okta_audience: str
    okta_jwks_url: str

    # The Databricks App this BFF calls server-to-server.
    databricks_app_url: str  # e.g. https://your-app-name.<region>.databricksapps.com
    databricks_host: str     # workspace host, e.g. https://adb-....azuredatabricks.net

    # This BFF's OWN Databricks service principal (M2M OAuth) -- distinct
    # from the Databricks App's own SP. Needs CAN_USE on the app. Not the
    # same identity as LAKEBASE_OWNER_EMAIL / lakebase_owner_pat.
    databricks_sp_client_id: str
    databricks_sp_client_secret: str

    # HMAC secret for this BFF's own session tokens (app/session.py) --
    # distinct from every Databricks/Apple credential.
    session_secret: str


def load_settings() -> Settings:
    """Read env vars leniently (empty-string defaults) so /health works even
    before the rest of the stack (Databricks SP, deployed app URL) exists.
    Each consuming module is responsible for checking it has what it needs
    at call time -- see apple_auth.verify_identity_token / databricks_client.
    """
    return Settings(
        auth_issuer=os.environ.get("AUTH_ISSUER", "apple").strip().lower(),
        apple_bundle_id=os.environ.get("APPLE_BUNDLE_ID", ""),
        okta_issuer=os.environ.get("OKTA_ISSUER", "").rstrip("/"),
        okta_audience=os.environ.get("OKTA_AUDIENCE", ""),
        okta_jwks_url=os.environ.get("OKTA_JWKS_URL", ""),
        databricks_app_url=os.environ.get("DATABRICKS_APP_URL", "").rstrip("/"),
        databricks_host=os.environ.get("DATABRICKS_HOST", "").rstrip("/"),
        databricks_sp_client_id=os.environ.get("DATABRICKS_SP_CLIENT_ID", ""),
        databricks_sp_client_secret=os.environ.get("DATABRICKS_SP_CLIENT_SECRET", ""),
        session_secret=os.environ.get("SESSION_SECRET", ""),
    )


settings = load_settings()

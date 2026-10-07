"""Call the Databricks App server-to-server, authenticated as this BFF's SP.

Why the trusted user header is safe: Databricks Apps ingress requires every
caller to already be a Databricks principal with CAN_USE on the app. As long
as CAN_USE is scoped only to this BFF's service principal (plus maybe a human
admin for debugging), any request that reaches the app came through this BFF.
The app can then trust X-App-User-Id without a second signature scheme.
If CAN_USE is granted more broadly, that assumption breaks.

Auth: this BFF has its own Databricks service principal (M2M OAuth), distinct
from the Databricks App's own SP (which talks to Lakebase, not inbound HTTP).
The SDK's Config.authenticate() mints/refreshes the OAuth token; that
workspace-scoped token is what Apps ingress accepts as Bearer.
"""
from __future__ import annotations

import httpx
from databricks.sdk.core import Config
from fastapi import HTTPException

from .config import settings
from .request_context import request_id

_config: Config | None = None


def _sp_config() -> Config:
    global _config
    if _config is None:
        if not (settings.databricks_host and settings.databricks_sp_client_id and settings.databricks_sp_client_secret):
            raise RuntimeError(
                "Databricks SP not configured -- set DATABRICKS_HOST, "
                "DATABRICKS_SP_CLIENT_ID, DATABRICKS_SP_CLIENT_SECRET"
            )
        _config = Config(
            host=settings.databricks_host,
            client_id=settings.databricks_sp_client_id,
            client_secret=settings.databricks_sp_client_secret,
        )
    return _config


async def call_app(method: str, path: str, *, user_id: str | None = None, **kwargs) -> httpx.Response:
    """Call `path` on the Databricks App as this BFF's SP.

    `path` should start with `/`, e.g. "/api/cards". Pass `user_id` (the
    resolved users.id) for any endpoint that touches a user-owned, RLS-scoped
    table -- it's passed through as a trusted header, see module docstring.
    Omit it only for endpoints that don't need a resolved user yet, like
    /internal/users/resolve itself.
    """
    if not settings.databricks_app_url:
        raise RuntimeError("DATABRICKS_APP_URL not configured")
    cfg = _sp_config()
    headers = dict(cfg.authenticate())  # {"Authorization": "Bearer <token>"}
    headers["X-Request-Id"] = request_id.get()
    if user_id:
        headers["X-App-User-Id"] = user_id
    url = f"{settings.databricks_app_url}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            return await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as e:
            # Every call site only handles a *returned* resp.status_code >= 400 --
            # an uncaught timeout here propagated as an unhandled 500 with no
            # translation, leaving databricks-app's own scan (e.g. the player-
            # intelligence refresh, which has no cancellation on client
            # disconnect) running orphaned server-side. 504 tells the client
            # the upstream just didn't answer in time, distinct from a 502.
            raise HTTPException(504, f"databricks-app request to {path} timed out") from e
        except httpx.HTTPError as e:
            raise HTTPException(502, f"databricks-app request to {path} failed: {e}") from e

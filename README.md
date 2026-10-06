# ios-lakebase-bff

A small public Backend-for-Frontend (BFF) that sits between a native iOS app and a Databricks App backed by Lakebase.

Databricks Apps ingress only admits Databricks principals with `CAN_USE` on the app. An Apple or Auth0 identity token is not one of those. This service is the bridge:

1. Verify the consumer identity token (Apple and/or Auth0 / Okta CIAM).
2. Resolve (or create) a stable internal `user_id` by calling the Databricks App as a machine identity.
3. Issue an app-owned session JWT for the phone.
4. On later calls, validate that session and call the Databricks App with a Databricks OAuth token plus a trusted `X-Cardshop-User-Id` header.

The BFF never connects to Lakebase. Row-level security stays in the Databricks App / Postgres path.

```
iOS  -- Apple or Auth0 ID token -->  BFF (public HTTPS)
       -- verify, resolve user, issue session -->
       -- Databricks SP + X-Cardshop-User-Id -->
Databricks App  -- set_config(app.user_id) + RLS -->  Lakebase
```

This repository is a **sanitized public sample** of that BFF. It is not the production deployment, and it intentionally omits live hosts, workspace IDs, secrets, and tenant-specific deploy notes.

## What this service does

| Route | Auth | Role |
|---|---|---|
| `GET /health` | none | Liveness |
| `GET /me` | Bearer Apple or Auth0 ID token (`X-OIDC-Nonce` when the token has a nonce) | Verify IdP → `POST /internal/users/resolve` on the app → return `user_id` + `session_token` |
| Everything else | Bearer BFF session JWT | Proxy to the Databricks App with `X-Cardshop-User-Id` set |

Proxy routes in `app/main.py` mirror a inventory-style FastAPI app (cards, research, players). Treat them as examples of the session → trusted-header → app pattern. Your backend paths can differ; the identity edge should not.

## Trust model (load-bearing)

- **Only this BFF's Databricks service principal** should have `CAN_USE` on the target app (plus admin identities for manage). That is what makes `X-Cardshop-User-Id` trustworthy.
- The BFF holds **no** Lakebase credentials.
- Face ID (or similar biometrics) on the phone unlocks a retained refresh / session secret. It does not authenticate to Databricks.
- Production apps should reject a missing user header rather than falling back to a bootstrap tenant. That check lives in the Databricks App, not here.

## Configuration

All environment-specific values come from env vars (see `.env.example`). Nothing tenant-specific belongs in source.

| Variable | Purpose |
|---|---|
| `AUTH_ISSUER` | `apple`, `okta`, or `both` |
| `APPLE_BUNDLE_ID` | Apple `aud` |
| `OKTA_ISSUER` / `OKTA_AUDIENCE` / `OKTA_JWKS_URL` | Auth0 or Okta OIDC |
| `DATABRICKS_APP_URL` | Target Databricks App base URL |
| `DATABRICKS_HOST` | Workspace host for M2M OAuth |
| `DATABRICKS_SP_CLIENT_ID` / `DATABRICKS_SP_CLIENT_SECRET` | BFF service principal |
| `SESSION_SECRET` | HMAC key for session JWTs |

Generate a session secret with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill with your own values
uvicorn app.main:app --reload --port 8080
curl localhost:8080/health
```

You need a Databricks App that exposes at least `POST /internal/users/resolve` and RLS-scoped inventory routes, plus a service principal with `CAN_USE` on that app only.

## Deploy sketch

Any public HTTPS host that can hold secrets works (Azure Container Apps, Cloud Run, Fly.io, etc.). Pattern:

- Public ingress to this service only.
- Secrets for `DATABRICKS_SP_CLIENT_SECRET` and `SESSION_SECRET` from a secret store / managed identity.
- Fail startup (or at least fail `/me` and proxies loudly) if those secrets are empty in production.
- After a failed platform deploy, confirm the **running image** before debugging auth. Some platforms leave a placeholder image active when a source deploy fails partway.

This sample does not include a production Dockerfile pipeline or IaC on purpose. Wire it to whatever you already operate.

## Layout

```
app/
  main.py              # /health, /me, session gate, proxies
  identity_auth.py     # Apple vs Auth0 issuer routing
  apple_auth.py        # Apple JWKS verify
  okta_auth.py         # Auth0 / Okta JWKS + nonce rules
  session.py           # HS256 session JWT
  databricks_client.py # M2M call + X-Cardshop-User-Id
  config.py            # env-driven settings
  request_context.py   # request id / user correlation for logs
```

## Security notes for reuse

- Do not commit `.env`, PATs, SP secrets, or real workspace hosts.
- Scope `CAN_USE` tightly. A broad grant turns the trusted header into a free tenant switch.
- Prefer rejecting missing `X-Cardshop-User-Id` in the Databricks App over a bootstrap UUID fallback.
- Session tokens here are long-lived HMAC JWTs (`sub` / `iat` / `exp`). Harden for production (shorter TTL, `iss`/`aud`, revocation) if you ship this pattern broadly.
- Auth0 interactive login ID tokens carry a `nonce`; refresh-token ID tokens often do not. The verifier matches that behavior.

## License / status

Reference sample for the architecture described in the accompanying Lakebase + iOS identity write-up. Not an official Databricks product, and not a drop-in production deployment of a live consumer app.

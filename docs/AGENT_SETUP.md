# Agent setup runbook

This document is written for a coding agent that is installing
`ios-lakebase-bff` for a user. It turns the architecture into an ordered,
verifiable setup procedure.

This repository contains the public BFF. It does **not** contain the
downstream Databricks App, its Lakebase schema, or the native iOS client. A
complete installation therefore has three deployable parts:

1. A compatible Databricks App connected to Lakebase.
2. This BFF on public HTTPS outside Databricks.
3. A native client configured for Apple and/or Auth0.

The BFF never connects to Lakebase.

## Non-negotiable agent rules

1. Never select a Databricks CLI profile automatically.
2. Run `databricks auth profiles`, show every profile and workspace URL, and
   ask the user which profile to use. Do this even when only one profile is
   listed.
3. Pass `--profile <PROFILE>` on every Databricks CLI command.
4. Ask whether to reuse an existing Lakebase project, branch, and database or
   create new ones. Never create or delete Lakebase resources silently.
5. Do not grant `CAN_USE` to all account users. It must be restricted to the
   BFF service principal, with named admins retaining `CAN_MANAGE`.
6. Keep the BFF service principal and the Databricks App service principal
   separate.
7. Never give the BFF Lakebase credentials.
8. Never print, log, paste into chat, or commit an OAuth client secret,
   `SESSION_SECRET`, ID token, refresh token, or BFF session token.
9. Never use a personal access token for the deployed BFF.
10. Stop before destructive operations, production migrations, public
    exposure, or paid resource creation unless the user explicitly approved
    them.
11. Use placeholders in committed files. Store live values only in an
    untracked `.env` or the deployment platform's secret store.
12. Treat a missing `X-App-User-Id` as unauthorized in production. Never
    substitute a bootstrap UUID.

## Architecture invariant

Keep this path intact:

```text
iOS
  -> Apple or Auth0 ID token
Public BFF
  -> Databricks OAuth as the BFF service principal
  -> X-App-User-Id after identity resolution
Databricks App
  -> Lakebase OAuth as the App service principal
  -> set_config('app.user_id', ...)
Lakebase
  -> row-level security
```

There are two machine identities:

- **BFF service principal:** calls one Databricks App and has `CAN_USE` on
  that app. It has no Postgres role.
- **App service principal:** injected by Databricks Apps and bound to a
  Lakebase Postgres role with `bypassrls = false`.

An optional jobs or migration role may be elevated. It must never serve the
interactive request path.

## Phase 0: Collect decisions before changing anything

Ask the user for these choices:

- Databricks CLI profile.
- Existing or new Databricks App.
- Existing or new Lakebase Autoscaling project.
- Existing or new branch and database.
- Schema name owned by the application.
- Allowed identity providers: `apple`, `okta`, or `both`.
- Public BFF hosting platform.
- Environment name: development, staging, or production.
- Whether shared Unity Catalog data or Volumes are also required.

Record these non-secret values:

```text
PROFILE=
ENVIRONMENT=
APP_NAME=
APP_URL=
APP_SP_APPLICATION_ID=
LAKEBASE_PROJECT_ID=
LAKEBASE_BRANCH_ID=
LAKEBASE_ENDPOINT_ID=
LAKEBASE_DATABASE=
APP_SCHEMA=
BFF_SP_NUMERIC_ID=
BFF_SP_APPLICATION_ID=
PUBLIC_BFF_URL=
AUTH_ISSUER=
```

Do not record secret values in this file.

## Phase 1: Preflight

### 1.1 Check local tools

```bash
python3 --version
databricks --version
docker --version
git status
```

Requirements:

- Python 3.10 or newer. Python 3.12 is recommended.
- Databricks CLI 0.294.0 or newer.
- Docker only when the chosen host requires a local image build.

If the Databricks CLI is missing or older than 0.294.0, stop and install or
upgrade it before continuing.

### 1.2 Select a Databricks profile

```bash
databricks auth profiles
```

Show the complete result to the user and wait for their selection. Then verify
the selected profile:

```bash
databricks current-user me --profile <PROFILE>
databricks apps list --profile <PROFILE>
databricks postgres list-projects --profile <PROFILE>
```

Checkpoint:

- The current user and workspace are the ones the user intended.
- Every subsequent command includes `--profile <PROFILE>`.

## Phase 2: Prepare Lakebase

Lakebase is Autoscaling-only. Use `databricks postgres`, not the retired
`databricks database` commands.

### 2.1 Reuse or create

For an existing project:

```bash
databricks postgres list-projects --profile <PROFILE>
databricks postgres list-branches projects/<PROJECT_ID> --profile <PROFILE>
databricks postgres list-endpoints \
  projects/<PROJECT_ID>/branches/<BRANCH_ID> \
  --profile <PROFILE>
databricks postgres list-databases \
  projects/<PROJECT_ID>/branches/<BRANCH_ID> \
  --profile <PROFILE>
```

For a new project, only after explicit approval:

```bash
databricks postgres create-project <PROJECT_ID> \
  --json '{"spec":{"display_name":"<DISPLAY_NAME>"}}' \
  --profile <PROFILE>
```

A new project creates a `production` branch, a primary read-write endpoint,
and the default `databricks_postgres` database. Discover the generated
resource IDs rather than guessing them.

### 2.2 Use a branch for schema work

For an existing production database, create a temporary branch and validate
all DDL there before production:

```bash
databricks postgres create-branch projects/<PROJECT_ID> <TEST_BRANCH_ID> \
  --json '{
    "spec": {
      "source_branch": "projects/<PROJECT_ID>/branches/<SOURCE_BRANCH_ID>",
      "ttl": "14400s"
    }
  }' \
  --profile <PROFILE>
```

Do not delete the production branch. Do not apply untested DDL directly to
production.

### 2.3 Create the application schema and tenancy model

The downstream Databricks App must own this work. The minimum logical schema
is:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS app_data;

CREATE TABLE IF NOT EXISTS app_data.users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  apple_sub text UNIQUE,
  okta_sub text UNIQUE,
  email text,
  display_name text,
  last_login_at timestamptz NOT NULL DEFAULT now(),
  CHECK (apple_sub IS NOT NULL OR okta_sub IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS app_data.items (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES app_data.users(id),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE app_data.items ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_data.items FORCE ROW LEVEL SECURITY;

CREATE POLICY items_by_user ON app_data.items
  USING (
    user_id = current_setting('app.user_id', true)::uuid
  )
  WITH CHECK (
    user_id = current_setting('app.user_id', true)::uuid
  );

CREATE INDEX IF NOT EXISTS items_user_id_idx
  ON app_data.items (user_id);
```

Adapt names to the application. Every user-owned table needs `user_id`, an
index, RLS enabled, and both `USING` and `WITH CHECK`.

The identity resolver runs before `user_id` exists, so the `users` table is
not scoped by `app.user_id`. Protect it with Apps ingress and expose it only
through the internal resolver route.

Checkpoint on the test branch:

- No `app.user_id` returns zero tenant rows.
- A wrong `app.user_id` returns zero rows.
- The correct value returns only that user's rows.
- Cross-user `INSERT` is rejected.
- Views over tenant tables use invoker security and do not bypass RLS.

## Phase 3: Deploy or adapt the Databricks App

This repository cannot perform this phase by itself. Stop if no compatible
Databricks App codebase exists.

The downstream app must implement:

### 3.1 Internal identity resolver

```http
POST /internal/users/resolve
Authorization: Bearer <Databricks OAuth token from BFF SP>
X-Request-Id: <uuid>
Content-Type: application/json
```

Accept exactly one provider subject:

```json
{
  "apple_sub": null,
  "okta_sub": "<provider-subject>",
  "email": "person@example.com",
  "email_verified": true,
  "display_name": "Example Person"
}
```

Required behavior:

1. Look up the provider subject.
2. Link by email only when the provider says the email is verified and exactly
   one existing row matches.
3. Never overwrite a different provider subject.
4. Never guess when email matches are ambiguous.
5. Otherwise create a new user.
6. Return `{ "user_id": "<uuid>", "created": true|false }`.

Do not require `X-App-User-Id` on this route. Resolving that value is the
route's purpose.

### 3.2 Request identity guard

`X-App-User-Id` is this application's internal user UUID (`users.id`). The BFF
mints it through `/internal/users/resolve`. It is not an Apple or Auth0
`sub`, not a Databricks principal, and not a product brand. Rename the header
if you want; keep the meaning: one stable app-owned id per person.

All user-owned routes must:

1. Require `X-App-User-Id`.
2. Parse it as a UUID.
3. Return 401 when it is missing or malformed.
4. Never use a default or bootstrap tenant in production.

### 3.3 Lakebase connection scope

The app, not the BFF, connects to Lakebase as the App service principal. Before
any tenant query:

```sql
SELECT set_config('app.user_id', %s, false);
```

For session pooling, clear the value in `finally` before returning the
connection:

```sql
RESET app.user_id;
```

For transaction pooling, run the whole request in one transaction and use:

```sql
SELECT set_config('app.user_id', %s, true);
```

Do not mix these modes. `false` is session-scoped and sticky. `true` is
transaction-local and disappears at commit.

### 3.4 Deploy and discover the App service principal

Use the app repository's deployment process. Then inspect the deployed app:

```bash
databricks apps validate --profile <PROFILE>
databricks apps list --profile <PROFILE>
databricks apps get <APP_NAME> --profile <PROFILE>
```

Record:

- App name.
- App HTTPS URL.
- App service principal application ID.

Do not confuse the App service principal with the BFF service principal that
will be created later.

## Phase 4: Bind the App service principal to Lakebase

Create a Postgres role for the **App** service principal:

```bash
databricks postgres create-role \
  projects/<PROJECT_ID>/branches/<BRANCH_ID> \
  --role-id <APP_SP_APPLICATION_ID> \
  --json '{
    "spec": {
      "identity_type": "SERVICE_PRINCIPAL",
      "postgres_role": "<APP_SP_APPLICATION_ID>",
      "auth_method": "LAKEBASE_OAUTH_V1"
    }
  }' \
  --profile <PROFILE>
```

Do not set membership in `DATABRICKS_SUPERUSER`.

Verify the role:

```bash
databricks postgres list-roles \
  projects/<PROJECT_ID>/branches/<BRANCH_ID> \
  --profile <PROFILE>
```

Checkpoint:

- `identity_type` is `SERVICE_PRINCIPAL`.
- `bypassrls` is false.
- The role is not superuser.
- It has only the database, schema, sequence, and table privileges required by
  the app.

Apply ordinary Postgres grants using the migration identity:

```sql
GRANT CONNECT ON DATABASE <DATABASE> TO "<APP_SP_APPLICATION_ID>";
GRANT USAGE ON SCHEMA app_data TO "<APP_SP_APPLICATION_ID>";
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA app_data
  TO "<APP_SP_APPLICATION_ID>";
GRANT USAGE, SELECT
  ON ALL SEQUENCES IN SCHEMA app_data
  TO "<APP_SP_APPLICATION_ID>";
```

Add default privileges if future migrations create tables or sequences. Run
the RLS checkpoint again as the exact deployed app role, not a lookalike test
role.

## Phase 5: Create the BFF service principal

Create a second service principal:

```bash
databricks service-principals create \
  --display-name "<APP_NAME>-bff-<ENVIRONMENT>" \
  --profile <PROFILE>
```

The response contains:

- A workspace numeric `id`.
- An `applicationId` UUID.

Use the application ID as `DATABRICKS_SP_CLIENT_ID`. Create the OAuth secret
against the numeric ID:

```bash
databricks service-principal-secrets-proxy create <BFF_SP_NUMERIC_ID> \
  --lifetime 31536000s \
  --profile <PROFILE>
```

Capture the returned secret directly into the deployment secret store. Do not
write it to a tracked file. If it is exposed, revoke it and create another.

## Phase 6: Restrict Apps ingress

Read the current ACL before changing it:

```bash
databricks apps get-permissions <APP_NAME> --profile <PROFILE>
```

Add `CAN_USE` for the BFF service principal:

```bash
databricks apps update-permissions <APP_NAME> \
  --json '{
    "access_control_list": [
      {
        "service_principal_name": "<BFF_SP_APPLICATION_ID>",
        "permission_level": "CAN_USE"
      }
    ]
  }' \
  --profile <PROFILE>
```

Use `update-permissions`. `set-permissions` replaces direct permissions and
can remove existing administrators.

Read the ACL again:

```bash
databricks apps get-permissions <APP_NAME> --profile <PROFILE>
```

Required result:

- Admins retain `CAN_MANAGE`.
- The BFF SP has `CAN_USE`.
- No broad account group has `CAN_USE`.
- No unrelated service principal has `CAN_USE`.

This ACL is what makes `X-App-User-Id` trustworthy. If access is widened,
the header design must be replaced with an independently authenticated
downstream assertion.

## Phase 7: Configure Apple and/or Auth0

### Apple

Create or reuse a Sign in with Apple capability for the native app. Set:

```text
AUTH_ISSUER=apple
APPLE_BUNDLE_ID=<native-app-bundle-id>
```

The BFF verifies Apple's issuer, JWKS signature, audience, expiration, issue
time, and subject.

### Auth0 or Okta CIAM

Create a Native application using Authorization Code with PKCE S256. Native
clients do not have a client secret.

Configure exact callback and logout URLs for the app. Then set:

```text
AUTH_ISSUER=okta
OKTA_ISSUER=https://YOUR_TENANT.auth0.com/
OKTA_AUDIENCE=<native-client-id>
OKTA_JWKS_URL=
```

For both providers:

```text
AUTH_ISSUER=both
```

`OKTA_JWKS_URL` may stay empty. The verifier derives Auth0
`/.well-known/jwks.json` or Okta Workforce `/v1/keys`.

Nonce behavior:

- Interactive Auth0 ID tokens contain a nonce. The client must send the same
  value in `X-OIDC-Nonce`.
- ID tokens obtained through refresh generally do not contain a nonce. The BFF
  accepts that path without the header.

## Phase 8: Configure this BFF locally

```bash
git clone https://github.com/toddcc1/ios-lakebase-bff.git
cd ios-lakebase-bff
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`:

```text
AUTH_ISSUER=<apple|okta|both>
APPLE_BUNDLE_ID=<bundle-id-or-empty>
OKTA_ISSUER=<issuer-or-empty>
OKTA_AUDIENCE=<native-client-id-or-empty>
OKTA_JWKS_URL=
DATABRICKS_APP_URL=<deployed-app-url>
DATABRICKS_HOST=<workspace-url>
DATABRICKS_SP_CLIENT_ID=<BFF_SP_APPLICATION_ID>
DATABRICKS_SP_CLIENT_SECRET=<secret>
SESSION_SECRET=<random-secret>
```

Generate the session secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Never accept an empty `SESSION_SECRET` or
`DATABRICKS_SP_CLIENT_SECRET` in a deployed environment.

Run:

```bash
uvicorn app.main:app --reload --port 8080
curl --fail http://localhost:8080/health
```

Expected:

```json
{"status":"ok"}
```

A malformed identity token must fail:

```bash
curl -i \
  -H 'Authorization: Bearer not-a-jwt' \
  http://localhost:8080/me
```

Expected: `401`.

## Phase 9: End-to-end validation

Use a real test user and a non-production Lakebase branch first.

### Identity exchange

Call `/me` with a real Apple or Auth0 ID token:

```bash
curl --fail \
  -H "Authorization: Bearer <ID_TOKEN>" \
  -H "X-OIDC-Nonce: <NONCE_IF_PRESENT>" \
  -H "X-Request-Id: <UUID>" \
  http://localhost:8080/me
```

Do not paste the real command or response into logs or chat. Capture the
returned BFF session token into a local shell variable without echoing it.

Verify:

- The provider token is accepted only for the configured issuer and audience.
- `/internal/users/resolve` returns an internal UUID.
- A BFF session is issued.
- Repeating the same login resolves the same user.
- An unverified or ambiguous email does not take over another account.

### Session request

Call one session-gated BFF route:

```bash
curl --fail \
  -H "Authorization: Bearer <BFF_SESSION_TOKEN>" \
  -H "X-Request-Id: <UUID>" \
  http://localhost:8080/cards
```

Verify:

- The BFF sends its Databricks OAuth token to Apps ingress.
- It adds `X-App-User-Id`.
- The Databricks App sets `app.user_id`.
- Lakebase returns only that user's rows.

### Negative isolation tests

All are required:

- No BFF session: 401.
- Invalid BFF session: 401.
- Wrong IdP issuer: 401.
- Wrong audience: 401.
- Wrong interactive nonce: 401.
- Missing downstream user header: 401 from the Databricks App.
- No `app.user_id`: zero tenant rows.
- Another user's UUID: zero tenant rows.
- Cross-user insert: rejected by RLS.
- Direct Apps request from an identity without `CAN_USE`: rejected at ingress.

Do not claim installation success until the negative tests pass.

## Phase 10: Deploy the BFF

Build the supplied Dockerfile:

```bash
docker build -t ios-lakebase-bff:<TAG> .
docker run --rm -p 8080:8080 --env-file .env ios-lakebase-bff:<TAG>
```

Deploy the immutable image to the user's chosen public HTTPS platform.

Public configuration:

- `AUTH_ISSUER`
- `APPLE_BUNDLE_ID`
- `OKTA_ISSUER`
- `OKTA_AUDIENCE`
- `OKTA_JWKS_URL`
- `DATABRICKS_APP_URL`
- `DATABRICKS_HOST`
- `DATABRICKS_SP_CLIENT_ID`

Secret configuration:

- `DATABRICKS_SP_CLIENT_SECRET`
- `SESSION_SECRET`

Production requirements:

- TLS only.
- Port 8080 routed to the container.
- `/health` as liveness.
- Secret-store references rather than plain deployment manifests.
- Logs must not contain authorization headers, ID tokens, session tokens, or
  refresh tokens.
- One BFF service principal per environment.
- Image pinned by digest or immutable tag.
- Confirm the active image after every failed deployment.

After deployment:

```bash
curl --fail https://<PUBLIC_BFF_HOST>/health
```

Then repeat Phase 9 against the public URL.

## Completion report

An agent should finish with a report containing only non-secret values:

```text
[ ] Databricks profile chosen by user
[ ] Lakebase project/branch/database selected by user
[ ] DDL tested on a branch
[ ] Databricks App deployed
[ ] App SP Postgres role has bypassrls=false
[ ] BFF SP created separately
[ ] BFF SP is the only non-admin identity with CAN_USE
[ ] Apple and/or Auth0 configured
[ ] BFF local health check passed
[ ] Identity exchange passed
[ ] Session request passed
[ ] Negative auth checks passed
[ ] RLS isolation checks passed as the deployed App role
[ ] Public deployment health check passed
[ ] No secrets or tenant-specific identifiers committed
```

Report blockers plainly. Do not weaken the trust model to make a smoke test
pass.

## Common failures

### Apps ingress returns 401 or 403

Check:

- The BFF is using the BFF SP, not the App SP.
- `DATABRICKS_HOST` is the workspace that owns the app.
- `DATABRICKS_APP_URL` is the intended app.
- The BFF SP has `CAN_USE`.
- Its OAuth secret is current.

### `/internal/users/resolve` returns 422

The BFF and Databricks App disagree on the resolver payload. Deploy matching
contracts before debugging Lakebase.

### Postgres says a provider column does not exist

Application deployment did not run the Lakebase migration. Code deployment
and database migration are separate operations.

### Every tenant can see every row

Stop immediately. Check the exact deployed App Postgres role. If
`bypassrls = true`, Postgres does not evaluate RLS at all.

### The next request inherits the previous user's identity

The pool returned a connection with session-scoped `app.user_id` still set.
Use `RESET app.user_id` in `finally`, or use a single transaction plus
transaction-local `set_config(..., true)`.

### Missing user header returns real data

The downstream app has a bootstrap fallback. Remove it for production and
return 401.

### Auth0 login works interactively but restore fails

Interactive ID tokens carry a nonce. Refresh-token ID tokens usually do not.
The BFF verifies a nonce when the signed token includes one.

### Health passes but sessions fail

`/health` deliberately performs no downstream call. Check non-empty
`SESSION_SECRET`, BFF SP credentials, App URL, and `CAN_USE`.

# Private API/feed connections

The platform provides a reusable **read-only JSON HTTPS API/feed connector**.
It supports bearer tokens, API keys in an approved header, and public feeds. It
supports fixed endpoints with no pagination, numbered pages, or opaque cursors.
OAuth consent/token refresh, provider request signing (including provider-specific
Amazon signing), CSV/XML/RSS feeds and arbitrary provider actions need dedicated
adapters. An affiliate account alone is not proof of API access.

## Platform setup

Apply migrations through **042** and restart API and worker with
`AI_AUTOMATIONS=1` and `AI_DURABLE_TASKS=1`. Grant `feed.read` to each intended app
using the existing operator app catalogue. Preserve its other tool grants.
The worker and API must use identical profiles and the private feed keyring.

Configure a **separate** 32-byte AES-GCM key using the deployment secret store:

- `AI_FEED_ENCRYPTION_KEY_ID`: current key ID.
- `AI_FEED_ENCRYPTION_KEYS`: JSON mapping key IDs to base64-encoded 32-byte keys.

Keep old keys while old connection records use them. Saving a credential again
re-encrypts it with the current key. Losing all old keys requires reconnecting;
changing an environment variable alone does not re-encrypt existing records.
Secrets are bound cryptographically to tenant, app, owner and connection ID.
Database copies cannot move a credential to another user's connection. Restrict
DB access and run with non-superuser, non-BYPASSRLS roles in production.

Configure `AI_FEED_PROFILES` as an app-ID-to-profile mapping:

```json
{
  "REPLACE_WITH_APP_UUID": {
    "catalogue": {
      "name": "Approved catalogue feed",
      "url": "https://api.example.com/catalogue",
      "auth": "bearer",
      "items_pointer": "/items",
      "fields": {
        "source_url": "/url",
        "observation_id": "/id",
        "observed_at": "/observed_at",
        "price": "/price",
        "currency": "/currency",
        "fees_known": "/fees_known"
      },
      "pagination": "cursor",
      "page_parameter": "cursor",
      "next_pointer": "/next_cursor",
      "max_pages": 3,
      "max_items": 10,
      "max_age_seconds": 3600,
      "require_price": true,
      "setup_help": "Obtain approved API access from the provider and enter its API key.",
      "alternatives": ["Request the provider's authorized export feed."]
    }
  }
}
```

The example endpoint is a placeholder, not a live installed integration. Profile
URLs are approved by the operator; consuming apps and end users cannot change
network destinations. URLs must be HTTPS on port 443, without credentials, query
parameters or fragments. Secrets must never go in profiles or request arguments.
For header authentication set `auth` to `header` and e.g. `auth_header` to
`X-API-Key`. `none` requires no credential.

Mappings are JSON pointers relative to each item. Required fields are source URL,
observation ID and original observation timestamp; the connector never substitutes
request time for missing provider observation time. Omit optional mappings if the
provider has no such field; missing fees then remain unknown. Numeric strings are
not guessed into prices. For numbered pages, set `pagination: page`, choose the
page parameter, and point `next_pointer` to a boolean has-more field. Pages start
at 1. Cursor pagination accepts a bounded opaque string or null/empty at the end;
it never follows a response-supplied URL. Repeated cursors fail closed.

A profile fingerprint pins endpoint/auth/mapping/limits. Editing a profile requires
the owner to review and reconnect; previously queued actions cannot silently send
credentials to a different endpoint. Existing agents pin connection revision and
profile hash; regenerate their saved inputs after reconnecting.

## App integration flow

Use the app's existing private service credential and authenticated
`X-External-User-ID`. End-user browsers should call their own app backend, never
receive a platform service key. Required scopes: `tasks:read`, `tasks:write`,
`agents:run`. Normal saved-agent and schedule APIs require their usual additional
scopes. Only `feed.read` is needed for the feed endpoints; it internally uses the
connector health runtime, without granting broader tools.

1. `GET /v1/connectors/feeds/profiles` returns approved profile names, endpoints,
   fingerprints, setup guidance, alternatives, limits and whether encryption is
   configured. No credentials are returned.
2. `POST /v1/connectors/feeds` with `profile` and its exact `profile_hash` creates
   an owner-scoped connection, or returns the existing one for that profile.
3. `PUT /v1/connectors/feeds/{id}/credentials` with `expected_revision`, the reviewed
   `profile_hash`, and `secret` encrypts the credential and **atomically queues a
   worker test**. Public feeds can instead use the test endpoint without a secret.
   Validation errors never echo the submitted secret. Responses return IDs and
   revision, not encrypted or plaintext credential material.
4. `GET /v1/connectors/feeds` returns status and last task ID/status. Use
   `GET /v1/tasks/{id}` for test results and structured evidence.
5. `POST /v1/connectors/feeds/{id}/test` accepts `expected_revision` and an
   `idempotency_key`; repeated keys on the same revision reuse the task.
6. `DELETE /v1/connectors/feeds/{id}` disables the connection, removes its credential
   and increments its revision so queued work cannot use it.

The credentials operation uses optimistic revision checks: if a response is lost,
reload the connection's revision/status before retrying. If another task is active,
the API returns 409 and rolls back the credential change and queued test together.
Saving a new credential clears its previous authorization block but preserves the
provider cooldown; the test is scheduled no earlier than that cooldown.

Statuses include AUTHORIZATION_REQUIRED, TEST_REQUIRED, TESTING, READY,
EVIDENCE_REQUIRED, BLOCKED, RATE_LIMITED, UNAVAILABLE, BROKEN, WAITING_RETRY,
CONFIGURATION_REQUIRED, TEST_FAILED and DISCONNECTED. READY requires usable,
recent evidence; a successful HTTP response alone is insufficient.
Credentials can still expire after a successful test. The next 401 sets
AUTHORIZATION_REQUIRED; automatic refresh is not part of this version.

The local operator UI is **Agent Workspace → API and feed connections**, at
`http://127.0.0.1:4061/connectors`. It uses the console app's own identity and approved
profiles, not another consuming app's connections. It is loopback-only with origin
and explicit-intent checks. It does not expose an operator console publicly.

## Reading and scheduling later

Use `feed.read` version `1` with these concrete arguments in an ordinary saved agent:

```json
{
  "connection_id": "REPLACE_WITH_CONNECTION_UUID",
  "revision": 2,
  "profile_hash": "REPLACE_WITH_REVIEWED_PROFILE_HASH"
}
```

No secret is included. This runs through the normal task worker, connector report
history, quotas and per-source cooldowns. The source is the connection UUID. Save
and schedule this agent using the existing orchestration APIs. Feed setup does not
create an indefinite schedule; scheduled runs remain bounded and require approval
of the exact automation definition. See [connector checks](CONNECTOR_CHECKS.md).

Results include public URLs, observation times and IDs, optional observed prices,
unknown fees and validation issues. `item_limit_reached`/`page_limit_reached` means a
bounded partial read, not complete provider coverage. Failed later pages do not
silently certify the earlier partial data as complete. There is no invented total
cost when delivery/other fees are unknown.

## Request safeguards and limits

- Resolve endpoint DNS once, reject non-global or mixed public/private answers,
  and pin the connection to the checked IP while retaining the TLS server name and
  certificate verification. Redirects, environment proxies and provider next-URLs
  cannot redirect credentials elsewhere.
- GET only. 401/403/redirect/429 failures do not retry. Transient network errors and
  selected 5xx/408/425 responses get at most three attempts per page. Retry-After
  returns a cooldown instead of sleeping in the worker. Each page/retry reserves
  a request from the app's `policy.feed_requests_per_hour` allowance (default 60;
  zero prevents HTTP requests). Existing check/run quotas also apply.
- Maximum five pages, ten observations, 256 KiB decoded JSON per response,
  five-second transport timeouts, and an 18-second feed execution deadline.
  Compressed responses are rejected; no large decompression is attempted.
- Provider bodies/errors are never logged or returned. Mapped observations are
  schema-checked, and responses containing the submitted secret are rejected.
  Providers still receive the credential for their approved endpoint as intended.
- These are request bounds, not a promise that provider use is free. Provider
  pricing, subscription/access eligibility and approved credentials remain separate.

## Tested scope

The suite exercises the real HTTP API, encrypted database storage, durable worker,
refresh/retest, stale configuration and disconnect fencing, plus mock HTTPS
transport for pagination/retries/statuses, response limits, source IP pinning,
quota enforcement and secret suppression. Local screen routing and CSRF/origin
checks are covered. It does not claim a live Amazon/retailer integration or bypass
blocked sources. DealAgent code and its existing integrations remain unchanged.

## Guided setup, recovery and schedule controls

The local connection screen now shows recovery instructions and lets the operator:

1. Choose the approved source and save its private key (which queues a test).
2. Inspect the worker's evidence and wait for READY.
3. Enter an interval and maximum number of checks; prepare a draft schedule.
4. Review the exact connection revision, frequency and limit, then activate it.
5. Pause/archive schedules or inspect their recent task runs.

The UI composes the existing `/v1/task-agents` and `/v1/orchestration/automations`
APIs; it introduces no second scheduler. Approval uses the returned immutable
`definition_hash`. Calls and retries consume the existing application quotas.
A deferred occurrence still counts against maximum runs. There is no implicit
provider subscription, access grant or claim of zero provider costs.

Connection listings include owner-scoped `recovery_options` and `schedules` for
single-step `feed.read` agents, with their pinned connection arguments. Saving a
replacement credential or disconnecting pauses linked direct feed/check schedules
and clears their approval. The user prepares a new schedule after testing the new
revision; previously saved agent inputs are immutable. All old actions also fail
revision checks before network access. Changing operator profile configuration
requires reconnecting and reviewing the changed profile hash.

Local console schedules use its temporary service principal; stopping that
console revokes that principal and scheduled dispatch will pause. Use a consuming
app's persistent, scoped service credential for durable deployed schedules.

Profiles may contain `replaces` mappings to suggest an approved alternative for an
exact blocked tool/source; see `CONNECTOR_CHECKS.md`. Mappings are private to the
owning application. Profile changes alter its fingerprint and require reconnecting.

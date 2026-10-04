# Multi-app task API: first increment

The durable task API now supports operator-provisioned organisations, applications, scoped
service keys and app-bound external users. This applies to `/v1/tasks` only. Existing research
agent endpoints still require NRAIAlgo's signed delegation; service keys do not grant access there.
The default feature flag remains off, and no live browser adapter is installed.

## Installation and administration

Run the normal checksum-verified migrations before deploying API and workers together:

```sh
python -m agent_platform.migrate
```

New migrations add durable external-outcome tracking (007) and service identity tables (008).
Do not edit previously applied migrations. Pause old workers during this rollout: old binaries
do not enforce the new outcome marker. Back up the database before a production migration.

With `AI_PLATFORM_DATABASE_URL` set in the operator's environment:

```sh
python -m agent_platform.service_identity create \
  --tenant example-company --organisation 'Example Company' --app-name 'Customer Portal' \
  --scope tasks:read --scope tasks:write --days 30
```

This prints the new application ID, key ID and credential **once**. Store the credential in the
calling application's server-side secret store. Do not put it in browser JavaScript, URLs or logs.
Only a SHA-256 digest of the random 256-bit secret is stored in the database. Keys expire after
1–90 days. Rotation uses the same command with `--app-id <existing-app-id>`; this preserves user
mappings. After the caller switches keys, revoke the old one:

```sh
python -m agent_platform.service_identity revoke <key-id>
```

Administration requires direct database access and has no public provisioning endpoint. HTTPS
and deployment access controls are required for external callers. Database backups contain user
requests and adapter arguments; the profile vault and general redaction pipeline are not built.

## Identity and authorization

Each request carries:

```http
Authorization: Bearer apk_<key-id>.<secret>
X-External-User-ID: <the caller's authenticated end-user identifier>
```

The tenant and application come from the verified key, never a caller-supplied tenant header.
The platform maps `(application, external user ID)` to an internal owner UUID. Identical external
IDs in different apps do not share tasks, even within the same organisation. Key rotation within
an application preserves access. The caller must authenticate its users and derive their IDs
server-side; it must not forward an arbitrary ID selected by an untrusted client.

Scopes:

| Scope | Authority |
| --- | --- |
| `tasks:read` | Task details, task pages, audit event pages and streams |
| `tasks:write` | Create, plan, start, pause, resume, cancel, reconcile |
| `tasks:approve` | Record a human's explicit approval of an exact action |

Do not give approval scope to an autonomous planner. An integration holding that scope is trusted
to show the exact summary and collect an explicit human yes. The platform cannot prove the UI
collected consent; a platform-hosted approval UI and end-user authentication are still pending.
Approvals record both internal user and service-key principal. Other API mutation events also
record the principal. Revocation prevents new requests and is rechecked during streams; it does
not cancel previously authorized durable work. Use task pause/cancel for that purpose.

Isolation is enforced by application queries and tested across owners, apps and tenants.
PostgreSQL row-level security is **not** implemented in this increment.

## API contract

The runtime OpenAPI document is available at `/openapi.json`. Send JSON bodies for mutations.
External-user headers are required for service keys on every task endpoint.

| Endpoint | Contract |
| --- | --- |
| `POST /v1/tasks` | `{request, idempotency_key}`; same owner/key/request returns the existing task; changed request returns 409 |
| `GET /v1/tasks?limit=50&before=<cursor>` | `{tasks, next_cursor}`; cursor is the last task ID of the prior page; default 50, max 100 |
| `GET /v1/tasks/{id}` | Task, immutable saved plan, latest 100 events; no worker lease token |
| `PUT /v1/tasks/{id}/plan` | `{steps: [{tool, arguments}]}`; draft only; operator-installed adapter validates inputs |
| `POST /v1/tasks/{id}/start` | Start saved draft |
| `POST /v1/tasks/{id}/approve` | `{decision: "yes", action_hash}`; approval expires in 10 minutes and is consumed once |
| `POST /v1/tasks/{id}/{command}` | pause, resume, cancel, reconcile |
| `GET /v1/tasks/{id}/events?after=0&limit=100` | `{events, next_after}` in ascending event ID order |
| `GET /v1/tasks/{id}/stream?after=0` | SSE `task_event`, JSON event body, persistent event ID |

Task pages use `(created_at,id)` keyset ordering. Cursors are owner-scoped; another user's cursor
returns 404. This is a moving history, not a snapshot. New tasks appear on a fresh first page.

Streams close after approximately 25 seconds. Reconnect with `Last-Event-ID` or `after` to replay
committed events; deduplicate by ID. On `auth_required`, refresh authentication before reconnecting.
Use a fetch-based SSE client when custom authorization headers are needed. The server queries its
saved event log while a stream is active; it does not yet use database notifications or a broker.
Slow/disconnected clients can resume through event pagination. No database connection is held
while sleeping or waiting for the network.

Only creation has request idempotency-key replay. Other mutations enforce state transitions;
a repeated approval does not execute twice and returns 409 once accepted. If an HTTP response is
lost, fetch task state/events before deciding the next action. Never blindly replay a submission.

## Execution invariant

`chore_steps.outcome_pending` persists independently of task status and queue mode. Dispatch of
a SEND action and handoff of a USER_ONLY action set it. Pause, cancel, queueing reconciliation,
worker failure and lease expiry cannot clear it. Ordinary execution is rejected while it is set.
Only successful verification or evidence-backed non-submission clears it. Pending stop requests
are honored before exposing the next step. This retains the per-user active-task exclusion until
an uncertain external outcome is resolved.

The browser still needs local takeover and stale-command rejection. A database lease alone
cannot prevent a remote browser from acting after disconnection.

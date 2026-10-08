# Generic orchestration contract

The API is mounted at `/v1/orchestration` when `AI_AUTOMATIONS=1`. Run migrations
037–039 before starting API and worker. The existing task worker also ticks the
scheduler, evaluates completed work and delivers callbacks; no second queue or
broker is required. PostgreSQL owns the durable state.

## Authentication and identity

Use an app-scoped `Authorization: Bearer ...` credential and a backend-derived
`X-External-User-ID`. Never expose credentials in browser JavaScript or accept an
arbitrary user ID from an unauthenticated browser. Tenant and owner IDs are derived
from the authenticated application. The consumer retains its own user login.

## Agent manifests

`POST /agents` requires `agents:write` and accepts:

```json
{
  "idempotency_key": "agent-registration-001",
  "status": "ACTIVE",
  "manifest": {
    "schema_version": 1,
    "version": 1,
    "runtime": "durable_plan",
    "blueprint": {
      "name": "My observer",
      "description": "Read application observations",
      "steps": [{"tool": "app.observation", "version": "1", "arguments": {"id": "known-id"}}]
    }
  }
}
```

The tool must already be installed and granted to the app. Definitions are immutable;
register a new version to change arguments, capabilities or instructions. The existing
`/v1/task-agents/{id}/runs` executes the manifest. `PATCH /agents/{id}` accepts `status`
(`DRAFT`, `ACTIVE`, `PAUSED`, `DISABLED`, `ARCHIVED`). Dispatch and the adapter authority
check revalidate agent status. Archived agents cannot be reactivated.

## Scheduling and conditions

### Optional adaptive runtime

Grant `runtime.next` to an app and register a blueprint with it as the sole initial
step (version `1`, arguments `{ "goal": "A specific bounded request", "max_tool_calls": 2 }`).
The worker calls the configured model, validates one next tool against the live grant
catalogue, persists it and its arguments, executes it, and then reasons over the saved
result. At most four domain calls and the app's `max_plan_steps` limit apply. Every
model attempt reserves the existing daily app/user planning allowance. SEND actions
pause for exact approval; USER_ONLY actions remain human-only. A continuation cannot
append past existing saved steps or bypass the app's grants.

Results, preferences and reviewed memories are untrusted context. `memory_enabled`
in the manifest controls retrieval for the adaptive runtime; facts are never promoted
automatically. Bounded context avoids resending entire histories, and successful
identical tool calls are not repeated. The client renders domain results from the
saved steps, not numerical claims in model explanations.

### Creating a schedule

`POST /automations` requires `agents:run` + `tasks:write`. Supply `idempotency_key`
and `definition` with `agent_id`, `name`, `trigger`, optional `condition`, optional
`notification`, and `max_runs` (default 100). An interval is 60–604800 seconds; hourly,
daily and weekly are 3600, 86400 and 604800. `once` supports a timezone-aware `start_at`.
`event` pins `event_type` and `event_version`; replay starts after registration.

```json
{
  "agent_id": "owned-agent-uuid",
  "name": "Observe a value",
  "trigger": {"type": "interval", "interval_seconds": 3600},
  "condition": {"path": "result.products.*.price", "operator": "lt", "value": 60000,
                "freshness_path": "observed_at", "max_age_seconds": 86400},
  "notification": {"channel": "in_app", "title": "Your condition was met", "on": "crossing"},
  "max_runs": 168
}
```

Creation returns `DRAFT` and `definition_hash`. Display the exact definition before
`POST /automations/{id}/approve` with `{ "decision": "yes", "action_hash": "..." }`
using `tasks:approve`. Approval authorizes the saved interval, run cap and in-app
notification. SEND tools still require their existing per-action task approvals;
recurring approval never bypasses those gates.

Conditions traverse `result` (last step) or `steps` (all results), support bounded
array `*`, and `lt/lte/gt/gte/eq/exists`. Missing, invalid, nonnumeric or stale data
cannot match numeric conditions. `freshness_path` is relative to the containing
object. No code, template evaluation or arbitrary network access is possible.
`crossing` notifies on the first match and only again after a successful nonmatch.
Failed reads do not reset crossing state. Prices and fees are never inferred.

`GET /automations` lists saved definitions. `PATCH /automations/{id}` pauses,
disables or archives; activation requires approval again.
Queued runs also recheck approval at dispatch, and running adapters recheck it before
their effect. Revocation suppresses notification; uncertain external effects continue
to require reconciliation rather than blind retry. `/automations/{id}/runs`
links occurrences to ordinary task IDs and results. Runs are serialized using the
existing one-active-task-per-user constraint. A busy owner delays an occurrence;
quota is rolled back. Missed interval ticks coalesce rather than creating a catch-up storm.

## Events, preferences and notifications

* `POST /events`: versioned `event_type`, `version`, `correlation_id`,
  `idempotency_key`, `payload`. Producer uses `tasks:write`; source/tenant cannot be
  spoofed. Reusing a key with changed content returns 409.
* `GET /events?after=sequence&limit=50`: durable replay scoped to the owner.
* `PUT /preferences/{namespace}`: `{value, expected_version}` using `tasks:write`.
  Version 0 creates; stale updates fail 409. `GET` returns value/version. The most
  recent documents (at most 4000 bytes) are untrusted planning context, never grants.
* `GET /notifications`: durable in-app inbox with task evidence.
  `POST /notifications/{id}/read` marks a notification read idempotently.
* `GET /usage`: planning reservations, recorded model tokens, latency and automation
  limits. Unreported usage and monetary costs remain explicitly unknown.

## Callback delivery

An operator maps `AI_EVENT_WEBHOOK_DESTINATIONS` to exact app-specific HTTPS
destinations: `{ "APP_UUID": {"callback": {"url":"https://app.example/events",
"secret_env":"APP_CALLBACK_SIGNING_SECRET"}}}`. The signing secret must be at least
32 characters and stays in the deployment environment. The allowlist is the egress
trust boundary; only operator-controlled HTTPS endpoints belong in it.

`POST /webhooks` with `{destination,event_type,event_version,decision:"yes"}` requires
`tasks:approve`. Secrets and URLs are never supplied by the model. Future matching
events are queued atomically. `DELETE /webhooks/{id}` disables future delivery.
`GET /deliveries` shows queued, delivered, cancelled and dead attempts.

Callbacks carry `X-Agent-Timestamp`, `X-Agent-Signature` and `Idempotency-Key`.
Signature is `sha256=` + HMAC-SHA256(secret, timestamp + `.` + raw body). Verify raw
bytes, reject timestamps outside five minutes, and persist deduplication by event ID
before business effects. Delivery is **at least once**, retries at most five attempts
with exponential backoff; redirects are rejected. Failed deliveries remain `DEAD`
for inspection. `event_webhooks.verify` is the reference verifier.

## Database and deployment boundaries

New tables use composite ownership foreign keys plus forced RLS. Application queries
set transaction-local tenant, app and owner scope; the worker uses explicit internal
worker context. Production DB roles must be NOSUPERUSER and NOBYPASSRLS. Consuming
apps never receive platform SQL access. Tests verify RLS under an actual restricted
role without application WHERE predicates. Older channel/research tables retain
their existing authorization and are not retroactively declared RLS-protected.

Daily planning reservations remain enforced before a model call; failures count.
`automation_runs_per_day` is operator-configured per app (default 100). Schedules
also have a finite run count, at most 100 unarchived definitions per owner, bounded
payloads, lease recovery and existing task retries. No pricing assumptions or payment
provider auto-upgrades are introduced. `AI_MODEL_ROUTES` optionally maps task type
to provider/model (`{"research":{"nvidia":"model-id"}}`).
`AI_MODEL_MAX_CONTEXT_BYTES` bounds requests before any model call (default 100000).

DealAgent's development routes are restricted to the local development owner and
same-origin browser calls. Public multi-user deployment requires the consumer's
authenticated session adapter. The current release does not claim that installing
this module supplies a user login, live retailer access agreements, or production
backup/load-test certification.

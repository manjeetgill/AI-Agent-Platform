# Generic orchestration contract

The API is mounted at `/v1/orchestration` when `AI_AUTOMATIONS=1`. Run migrations
through 045 before starting API and worker. The existing task worker also ticks the
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

For domain jobs that finish later and workflows using earlier step results, see
[asynchronous jobs and workflows](ASYNC_JOBS_AND_WORKFLOWS.md). These use the same
task-agent API, queue and approval endpoints; no second job broker is required.

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

## Connector verification

Use `runtime.connector_check` with the existing agent/schedule APIs for granted
read-only adapter probes. Inventory, evidence history and source-resume APIs live
under `/v1/connectors`. See [connector checks](CONNECTOR_CHECKS.md) for contracts,
quota configuration, status semantics and setup examples.

Private JSON API/feed setup is available at `/v1/connectors/feeds`; see
[API/feed connections](API_FEED_CONNECTIONS.md). Saved credentials queue a durable
connection test and never appear in task arguments or API responses.
# Application task readiness (local and service clients)

`GET /v1/integrations/task-readiness` requires an application service key and
`X-External-User-ID`, with `tasks:read`. It makes no model, retailer, feed or
messaging calls, spends no provider quota, and does not queue tasks or resume sources.
The existing `/v1/integrations/readiness` channel inventory remains compatible and
now links to this task-specific endpoint.

Parameters:

- `required_tool` (repeat, at most 20): exact tools the app wants to execute.
  Without this parameter, execution readiness means at least one granted typed tool
  is supported by the worker fleet; inspect `tools` for each tool's availability.
- `planning=true` (default): also require planning scopes, model configuration and
  remaining daily planning allowance. Use `false` for an already saved plan.
- `source`: optional connector source, allowed only with exactly one required tool.
  Selecting a blocked or cooling-down source blocks this preflight. Other source
  failures are warnings, so they do not disable unrelated catalogue tasks.

Response includes `ready`, `status` (`READY`, `READY_WITH_WARNINGS`, `BLOCKED`),
checks with stable `code`, `status`, `summary` and `next_action`, missing scopes and
grants, installed tool versions, worker availability, credential expiry, planning
quota remaining, and bounded app/user-scoped connector/feed metadata.
`provider_checks_performed` and `model.live_verified` are always false. The latest
planning-call record is historical evidence, never proof of current provider access.
Even READY only indicates checked prerequisites; approvals, argument validation,
quota reservations and grants are enforced again during task execution.

Workers publish a database heartbeat every five seconds; after 45 seconds without
a heartbeat they are treated as offline. Graceful shutdown removes the heartbeat.
Since task claiming is not routed by adapter version, every fresh worker must match
a selected tool's API version. Server renderer availability requires its running loop
and configured local runtime files; the check does not launch Chromium. Customer
browser pairing is checked only when `browser.retailer_observe` is explicitly selected.

Missing prerequisites return HTTP 200 with `ready=false`. Invalid/expired credentials
return 401, insufficient read scope 403, invalid parameters 422, and unavailable
database/failed assessment 503. The response never returns keys, credential hashes,
private filesystem paths, worker hostnames, page text or another owner's reports.

Local operator command (private existing client file, no new credentials needed):

```sh
.venv/bin/python scripts/check_app_readiness.py \
  --client-file /private/path/agent-platform.json \
  --tool dealagent.catalog_search
```

Add `--no-planning` for saved plans or `--json` for the API report. To check a source,
replace the selected tool with `--tool retailer.observe --source croma`. Exit status is 0 when
prerequisites pass (possibly with warnings), 2 when blocked. Apply migration 045
and restart the API and worker together before using this endpoint.

## Local task monitor

Open `http://127.0.0.1:4060/monitor` to inspect saved task execution. This is an
operator screen for explicitly configured application/user connections. It is not
a public customer console or an unrestricted cross-tenant administrator view.

It shows status totals, paginated and status-filtered tasks, step results and source
links, elapsed time (including waits), recorded failures, and the latest 100 task
events. Separate panels show application readiness, recent source recovery reports,
and daily model usage. Missing usage/cost is displayed as unknown. Planning usage
is not attributed to individual tasks unless the task record contains it.

The screen refreshes every five seconds while visible. Pause, resume, cancel and
check-actual-outcome use the existing authenticated task APIs and their current
state checks. Read-only connections have no control buttons. Uncertain outcomes
never offer a blind retry; reconciliation verifies the real state. Cancellation
cannot undo a remote action. Approval stays in the originating application's UI.

Enable through private local configuration:

- `AI_LOCAL_MONITOR=1` (disabled by default).
- `AI_LOCAL_MONITOR_CONNECTIONS=/absolute/private/monitor-connections.json`.

The connections file contains references to existing private application clients:

```json
{"connections":[{"id":"dealagent","name":"DealAgent · local owner",
  "client_file":"/absolute/private/agent-platform.json"}]}
```

Each referenced client must contain `url` (local API on port 4060), `app_id`,
`credential` (existing service key), and `external_user`. Both files must be owned
by the current OS user with mode 0600 and must not be symlinks. Keep them outside
Git; never put credentials in HTML, browser storage or chat. Up to 30 explicit
connections are supported; each keeps its own app identity, user scope and key
permissions. The monitor does not grant new permissions or query all app users.
Only connection labels/IDs reach the selector, and only allowlisted task/health
operations are proxied. The listener must stay on loopback; do not publish this
operator endpoint through a reverse proxy or tunnel. Cross-origin requests are
rejected. Monitoring does not call model or retailer providers.

Additional API support (normal service authentication still applies):

- `GET /v1/tasks?status=RUNNING&limit=25&before=<task-id>` filters existing cursor
  pagination and includes step counts, current tool/summary, lease expiry and next
  queued attempt time.
- `GET /v1/tasks/summary` returns status counts for the authenticated app user.
- `GET /v1/connectors/reports?order=desc&limit=50` returns newest reports first,
  with `next_before` for earlier pages. Default ascending `after` pagination is
  unchanged. The monitor summarizes the latest report per source from these 50
  reports; older history remains available through the API.

Tests cover cross-app and cross-user isolation, revoked/read-only keys, private
file handling, forbidden proxy operations, task state controls and report
pagination. Run `scripts/test_orchestration_local.py tests/python/test_local_monitor.py
 tests/python/test_durable_tasks.py tests/python/test_connector_checks.py
 tests/python/test_task_readiness.py` with the project Python and
`node --test tests/node/monitor.test.mjs`.

## Local application onboarding and connection tests

Open `http://127.0.0.1:4060/monitor/onboard` on the operator computer. Enable
`AI_LOCAL_MONITOR=1`, configure its private connection index as described above,
and opt in with `AI_LOCAL_ONBOARDING=1`. Restart the API and worker together.
This trusted-machine operator interface must not be published or tunneled.
It creates application identities and grants authority; customer-facing apps
continue to authenticate through the scoped public API.

### Setup flow

1. Enter the new application's name and an initial external user ID from that app.
2. Select installed tools and scopes. Only `platform.connection_test` is selected
   initially. The default scopes support saving/running agents and reading/writing
   tasks; approval, channel, memory and knowledge permissions require explicit selection.
3. Set credential lifetime (1–90 days) and runtime quotas. Defaults are 25 planning
   calls per app/day, 5 per user/day, 10 plan steps, 30 connector checks/hour,
   30 feed requests/hour and zero scheduled runs/day. These are the existing
   enforced request quotas, not a monetary spending cap or a universal tool-call cap.
4. Create the connection. The service key is saved in a mode-0600 file under
   `applications/` beside the private monitor index. Only its path and non-secret
   settings are returned to the local UI. Load that file in the consuming backend.
5. Check configuration and run the worker test. Refresh its result until DONE.
   Readiness does not call paid providers. Model and business-tool acceptance must
   be tested separately with genuine inputs and expected outputs.

Each setup has a UUID request ID. App, key, policy and grants commit in one database
transaction. A private recovery file is written before commit; replay after an
interrupted response or monitor-index update recovers the same committed key.
Changing the settings for an existing request ID is rejected. Replaying a revoked
key cannot re-enable it. Existing app settings and connections are preserved.
This wizard creates new apps; use the existing operator credential rotation and
app-manifest commands to modify an existing identity. Reload the wizard to create
another app after a successful setup. The connection index supports 30 profiles.

### Public worker-test API

`POST /v1/integrations/connection-test` takes `{"test_id":"<uuid>"}` and returns
HTTP 202 with `task_id`, current status and `poll_url`. It requires a service key
with `tasks:read`, `tasks:write`, `agents:run`, a granted `platform.connection_test`
tool and `X-External-User-ID`. The app backend must derive that ID from its own
authenticated user. Never trust a user-supplied tenant or another user's ID.

The test writes a fixed READ action and its queue entry atomically. The normal
worker returns the same test UUID, receipt timestamp, PASSED and `provider_calls:0`.
Repeating a test UUID returns its saved task, without re-executing completed work.
There are at most 20 new worker tests per app user/day. An active task for that user
returns 409 rather than interrupting it. Revoked tool grants prevent execution.

A successful diagnostic proves API identity, database/task persistence, queueing,
worker execution and result retrieval. It does not verify model credentials,
retailer evidence, WhatsApp eligibility, phone authorization or callback delivery.
The setup screen separately shows configuration readiness and scoped callback
delivery history. Public callback destinations still require the existing approved
HTTPS destination/signing-secret setup; polling and task event streams work locally.

Backend examples: `examples/integration/connection_test.py` (Python/httpx) and
`examples/integration/connection_test.ts` (Node with TypeScript support). Both load
credentials from a private file, use finite request timeouts, preserve a printed
non-secret test UUID for retry, and poll the saved task for at most 30 seconds.
They never put keys in the frontend. Typical use:

```sh
.venv/bin/python examples/integration/connection_test.py /private/app-client.json
node --experimental-strip-types examples/integration/connection_test.ts /private/app-client.json
```

Run onboarding/isolation/recovery tests against a disposable database:

```sh
.venv/bin/python scripts/test_orchestration_local.py tests/python/test_local_onboarding.py
```

## Transactional email (phase 1)

The platform owns email delivery; consuming applications own recipients, consent,
verification/reset token generation, token validation, and business triggers. No
LLM call is needed. SMS and marketing preference management are later phases. Delivery
webhooks and provider unsubscribe suppression are covered in phase 2 below; this API rejects `category: marketing`.

Enable `AI_EMAIL_NOTIFICATIONS=1` on both API and task worker and apply migration
046. Grant `notifications:send` and/or `notifications:read` explicitly to the
application service key. Existing app keys gain no permissions automatically.
Use the normal server-side Bearer credential and `X-External-User-ID` headers.
Never call this API with a service key embedded in frontend code.

### Operator configuration

`AI_EMAIL_APPS` is a JSON object keyed by application UUID. Example entry:

```json
{
  "APPLICATION_UUID": {
    "provider": "mock",
    "from_email": "no-reply@example.com",
    "from_name": "Example App",
    "allowed_link_hosts": ["example.com"],
    "limits": {"daily": 20, "monthly": 100, "per_minute": 5}
  }
}
```

There is no implicit provider. For live delivery select `resend` or `brevo` and
add `key_env`, e.g. `EMAIL_RESEND_APP_KEY`. Set that variable only in the backend
secret manager/environment. A missing key never switches to mock or another
provider. Route changes do not redirect emails already queued; they enter
`CONFIGURATION_REQUIRED` until their original route is restored or they expire.

Set `AI_EMAIL_GLOBAL_LIMITS` to another limits object. Both app and global caps
apply. Defaults are 100/day, 1,000/month, 30 attempts/minute; daily/monthly zero
blocks new sends. Counts reset on UTC calendar boundaries. Requests and attempts
have separate reservations under the same caps; retries consume attempt capacity.
Limit waits expire with the email. These are count limits, not provider billing
estimates, and do not cover sends from other systems sharing the provider account.

Generate independent random 32-byte secrets privately for:

- `AI_NOTIFICATION_HASH_KEY`: base64 key for content-bound idempotency.
- `AI_NOTIFICATION_ENCRYPTION_KEYS`: JSON mapping key ID to base64 AES-256 key.
- `AI_NOTIFICATION_ENCRYPTION_KEY_ID`: active encryption key ID.

Keep old encryption keys until queued payloads are finished/expired. Keep the
hash key stable for idempotent replay. Store these outside Git and frontend code.
The local launcher uses `.runtime/local/config.json` (0600, ignored by Git).

### API and templates

`POST /v1/notifications/email` returns 202 with a durable notification record:

```json
{
  "idempotency_key": "b360f88c-7a57-40f2-9b8e-d704ebff6922",
  "channel": "email",
  "category": "transactional",
  "to": "test@example.invalid",
  "template": "system_notification",
  "template_version": 1,
  "variables": {"message": "Your requested export is ready."},
  "expires_in_seconds": 600
}
```

Persist and reuse the same UUID when retrying an uncertain API submission. Same
UUID/content returns the original record; different content returns 409. A new
UUID is a new message. Quota rejection returns 429 without queuing a message.

| Template | Required variables | Maximum queue lifetime |
| --- | --- | --- |
| `email_verification` | `url` | 24 hours |
| `password_reset` | `url` | 1 hour |
| `otp` | `code` (4–12 letters/digits) | 15 minutes |
| `price_drop` | `product`, `price`, `url` | 24 hours |
| `system_notification` | `message` | 24 hours |

Default queue lifetime is 10 minutes. Apps must align token validity and email
expiry themselves. URLs must be HTTPS on configured exact allowed hosts; HTML
values are escaped. Price-drop is for user-requested watch alerts, not campaigns;
the consuming app must check the user's preferences before requesting one.

Read endpoints under `/v1/notifications/email`:

- `GET /templates`: versioned fields and expiry bounds.
- `GET /configuration`: safe readiness/provider/limit metadata.
- `GET /usage`: application counts and this user's status counts.
- `GET /?limit=25&before=UUID`: this app/user's paginated records.
- `GET /{id}`: status and attempt history, no email body or recipient.

Local operator dashboard: `http://127.0.0.1:4060/monitor/email`. The selected
connection needs `tasks:read` plus `notifications:read`; browser code never gets
its key. The dashboard is read-only and shows the latest 25 records.

### Queue, retries and delivery meaning

Postgres is the atomic email outbox. Workers claim leased rows, reserve attempt
capacity, then execute the pinned adapter. Transient failures use bounded
exponential backoff with jitter, provider Retry-After, at most five attempts and
the request expiry. No external provider call runs inside the web request.

- `MOCK_SENT`: simulated only; no external request or charge.
- `ACCEPTED`: provider returned an ID; **inbox delivery is not confirmed**.
- `OUTCOME_UNKNOWN`: a send may have happened; do not create a replacement blindly.
- `CONFIGURATION_REQUIRED`: missing/changed route, keys or encryption setup.
- `FAILED` / `EXPIRED`: see redacted failure code and attempt history.

Resend retries reuse its idempotency key and identical rendered payload, bounded
to 23 hours from the first attempt (provider retention is 24 hours). Brevo retries
only definitely unconnected requests or rate-limit rejections; ambiguous responses
and interrupted sends stop as unknown. There is no cross-provider fallback.
Provider error bodies and exception text are not logged. Recipient, rendered
content, OTP and token links are encrypted while queued and cleared from active
rows at terminal state. Metadata stays for auditing; backup retention remains an
operator responsibility. Authentication and infrastructure logs must also avoid
request bodies and Authorization headers.

### Live provider setup and current free tiers

Checked official documentation on 2026-10-09. Local testing uses explicit mock
configuration and selects no production provider automatically.

- [Resend pricing](https://resend.com/pricing): free 3,000 emails/month with a
  100/day limit. Verify account limits before enabling sending.
- [Brevo free limits](https://help.brevo.com/hc/en-us/articles/208580669-FAQs-What-are-the-limits-of-the-Free-plan):
  300/day; unused daily allowance does not roll over.

Register your domain, then add a sending domain/subdomain to the chosen provider.
For [Resend domain verification](https://resend.com/docs/dashboard/domains/introduction),
copy its generated SPF/DKIM records into your DNS and wait for verification;
configure DMARC with your domain administrator. For
[Brevo authentication](https://help.brevo.com/hc/en-us/articles/12163873383186-Authenticate-your-domain-with-Brevo-Brevo-code-DKIM-DMARC),
use its generated Brevo code, DKIM and DMARC records. Do not invent or duplicate
SPF records; follow the provider's exact instructions for your sending setup.
Create a sending API key in the provider dashboard, save it privately, select the
verified sender and set conservative app/global limits below the account quotas.
Then send one explicitly authorized live test and check receipt and provider logs.

Adapter contracts: [Resend send API](https://resend.com/docs/api-reference/emails/send-email),
[Resend idempotency](https://resend.com/docs/dashboard/emails/idempotency-keys),
[Brevo send API](https://developers.brevo.com/reference/send-transac-email).
No real provider delivery is proven by mocked contract tests. Delivery callbacks and bounce/suppression handling are implemented in phase 2
below. Marketing consent UI and MSG91/DLT setup remain subsequent phases; no SMS traffic is supported by this phase.

## Email delivery callbacks and suppression (phase 2)

Migration 047 adds provider events and application-scoped suppression. The send
`status` remains separate from `delivery_status`; existing app integrations keep
working. `GET /v1/notifications/email/{id}` now includes redacted `delivery_events`,
and list/detail expose `delivery_status` and `delivery_at`. Usage includes delivery
counts for the connection's user and the application's suppressed-recipient count.

Delivery outcomes: `UNCONFIRMED`, `SENT`, `DELAYED`, `DELIVERED`, `FAILED`,
`HARD_BOUNCED`, `UNSUBSCRIBED`, `COMPLAINED`. Provider confirmation of delivery means
the receiving mail server accepted the message, not proof of reading or inbox
placement. Negative terminal outcomes take precedence over delivered/sent/delayed
regardless of arrival order; all matched events remain in the history. Sending
acceptance never fabricates delivery confirmation, including in mock mode.

### Operator setup

1. Deploy the API behind a permanent HTTPS endpoint. Expose only the webhook route
   through a reverse proxy if the rest of the operator workspace is private.
2. Add a provider webhook targeting:
   `https://YOUR_API/v1/notifications/email/webhooks/APP_UUID/resend` (or `/brevo`).
   Register separate application endpoints/secrets and preferably separate sending
   accounts/keys. Matching requires this app, provider message ID and recipient;
   a callback routed to the wrong app cannot update another app's jobs.
3. Set `AI_EMAIL_WEBHOOKS` privately on the API:

```json
{
  "APP_UUID": {
    "resend": {"key_env": "EMAIL_RESEND_WEBHOOK_SECRET"},
    "brevo": {"key_env": "EMAIL_BREVO_WEBHOOK_TOKEN"}
  }
}
```

Set only configured provider entries. The referenced values live in the server
secret store/environment, not this JSON or frontend. Callback configuration is
separate from `AI_EMAIL_APPS`, allowing callbacks from a previous provider after
switching the sending route. `configuration.webhook_configured` checks local
secret availability; it does not claim that the provider endpoint is connected.

- **Resend:** copy the endpoint's `whsec_...` signing secret privately. Subscribe
  to `email.sent`, `email.delivered`, `email.delivery_delayed`, `email.failed`,
  `email.suppressed`, `email.bounced`, `email.complained`. Verification uses the raw
  body, Svix HMAC signature, constant-time comparison, and a five-minute request
  timestamp tolerance. Retry deduplication uses `svix-id`.
  [Official verification](https://resend.com/docs/webhooks/verify-webhooks-requests),
  [Svix manual specification](https://docs.svix.com/receiving/verifying-payloads/how-manual),
  [Resend events](https://resend.com/docs/webhooks/event-types).
- **Brevo:** generate a separate random token of at least 32 characters. Configure
  webhook `auth` as `{"type":"bearer","token":"PRIVATE_VALUE"}` and save the
  same token in the referenced environment variable. This is not the Brevo sending
  API key. Subscribe to sent, delivered, deferred, softBounce, hardBounce, spam,
  invalid, blocked, error and unsubscribed in the provider's event selector. Payload
  event names use the documented forms such as `hard_bounce`. Deduplication uses
  message ID, event, UTC event time and an app-specific recipient HMAC; Brevo's `id`
  is not assumed to be a unique message event. Batches of up to 100 are accepted.
  [Brevo webhook authentication](https://developers.brevo.com/docs/secured-webhooks),
  [payload definitions](https://developers.brevo.com/docs/transactional-webhooks).

Secrets are mandatory; unsigned callbacks are rejected. Configure HTTPS at the
edge; Brevo bearer authentication does not independently sign the request body.
Keep API keys and webhook tokens out of URL query strings and proxy logs. Provider
payload bodies, subjects, recipient addresses and failure descriptions are never
saved in the event log. Only bounded identifiers, normalized outcomes, times and
app-specific recipient HMACs are retained.

### Reliability and suppression rules

Callbacks are committed before returning success. Events arriving before the send
response are held as redacted pending records and reconciled when the worker saves
the provider ID. Unknown/mismatched IDs never change a message by recipient alone.
Pending records expire after seven days; each app has a 10,000-record pending cap
(backpressure returns 503). An ambiguous send without any recorded provider ID may
remain `OUTCOME_UNKNOWN`; reconciliation by guesswork is deliberately unsupported.

Hard bounces, spam complaints and provider unsubscribe events suppress that
recipient across all owners/providers **within the same application**. Soft bounces
only record a delay. New send requests to a suppressed address return 409. Queued
work is stopped as `SUPPRESSED` and sensitive payloads cleared; previously uncertain
retries stay `OUTCOME_UNKNOWN` and are stopped too. Workers also recheck suppression
before claiming. Already-started network sends cannot be recalled. Suppression does
not auto-expire or disappear after a later delivered event. No public removal API
is provided; removal requires an operator review and evidence of corrected address
or renewed permission. This is not a marketing consent/preferences implementation.

The recipient hash uses `AI_NOTIFICATION_HASH_KEY`, lowercasing the email address
without alias/dot stripping. Keep that key stable; rotation requires migrating
idempotency and suppression data, not merely changing the environment variable.
Old pre-047 jobs can still receive events by their scoped provider ID; queued old
jobs acquire the recipient HMAC before execution. Suppression metadata remains
until an explicit retention/review decision, including in backup planning.

### Verification boundary

Automated tests use official-shaped fixtures and a published Svix signature test
vector, with mocked outbound HTTP. They cover forged/expired signatures, bearer
failures, deduplication, batches, callback-before-ack races, out-of-order delivery,
wrong-recipient/app isolation, hard-bounce and complaint suppression. No live email
or public webhook registration is performed by these tests. Before declaring live
tracking ready, configure the actual endpoint, send one authorized test, then verify
its callback appears in `delivery_events` and the local email dashboard.

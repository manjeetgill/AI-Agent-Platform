# Browser task revision: durable execution foundation

This revision implements the task lifecycle and worker contracts for personal browser chores.
It does not enable browser automation, payment, login, or live merchant requests. The Chrome
extension, tested site adapters, profile vault, planner UI, template builder and receipt UI
remain separate implementation steps. No production site adapters are registered.

## Architecture decision

PostgreSQL is the durable queue: `chore_jobs` and task state change in the same transaction.
There is no database/Redis dual write. Claiming a job locks both rows, creates an expiring
execution token and records dispatch before calling an adapter. This provides durable dispatch,
not exactly-once execution on external websites.

Migration `006_durable_tasks.sql` adds tasks, steps, approvals, jobs and audit events. A partial
unique index enforces one active task per tenant/user, including paused or uncertain tasks.
Drafts can coexist. Uncertain submissions keep the active-task lock until reconciliation.

## Lifecycle

1. Create a draft with an idempotency key. Changed request text with the same key is rejected.
2. Save a plan of at most 30 registered actions. The adapter validates arguments and generates
   the summary and risk classification. Clients cannot register tools, supply a risk downgrade,
   or execute arbitrary code. Each adapter must allow only its supported typed arguments.
3. Start the task. READ actions enter the queue; SEND waits for approval; USER_ONLY waits for the user.
4. Approval binds task, step, user and a canonical hash of tool, version, arguments, summary and
   risk. It expires after ten minutes and is consumed once at dispatch. Approval only writes
   queue state; it never runs a tool in the HTTP request. Plans are immutable after start.
5. Workers persist a verified step outcome and the next queue entry together.
6. Pause/cancel revoke execution authority and remove queued work and unused approvals. An
   already dispatched SEND becomes OUTCOME_UNKNOWN, including when cancellation was requested.
   Changing database state cannot undo an external submission.
7. Reconciliation reads real state without repeating submission. Unknown outcomes stay unknown.
   Confirmed non-submission returns to fresh approval or honors a pending pause/cancel request.
   USER_ONLY completion requires adapter verification; a user cannot mark a task paid by API.

## Crash and retry policy

Attempts have a 45-second lease and unique token; adapters are limited to 30 seconds. Revoked
or expired tokens cannot commit results. READ failures retry up to three times with bounded
backoff. SEND failures and expired SEND leases become OUTCOME_UNKNOWN, never blind retries.
Workers reap expired leases and claim saved work on restart. Changed adapters fail closed.

The future extension must recheck authorization before acting, reject old tokens, stop locally
on takeover, and suspend observation during login, OTP and payment. The database alone does not
provide those browser-side guarantees. Never infer that a website action failed from lease expiry.

## API and authentication

`AI_BROWSER_TASKS=1` enables `/v1/tasks`; it is off by default. Existing signed delegations
authenticate requests, and every user operation checks tenant and owner. Standalone login and
extension/device pairing are not implemented here. NRAIAlgo's current facade does not proxy tasks.

- `POST /v1/tasks`: request and idempotency_key
- `GET /v1/tasks`: most recent 100 owned tasks
- `GET /v1/tasks/{id}`: plan and most recent 100 audit events
- `PUT /v1/tasks/{id}/plan`: registered tool names and arguments
- `POST /v1/tasks/{id}/start`
- `POST /v1/tasks/{id}/approve`: `decision: "yes"` and exact `action_hash`
- `POST /v1/tasks/{id}/pause`, `/resume`, `/cancel`, `/reconcile`

Workers accept an explicit Registry of operator-installed adapters. The CLI refuses to start
with an empty registry. Never load arbitrary tools from model output or user requests. READ
adapters and reconciliation methods must be audited for absence of side effects. SEND summaries
must include actual destination, fields, commitments, amount and currency. Changed details
invalidate approval. Payment, login and secret entry must always be USER_ONLY in site adapters.

## Evidence and remaining scope

Audit entries record transitions, hashes, attempts and approval identity. They do not capture
raw pages or screenshots. Adapter evidence must be bounded and redacted. Requests and arguments
are not backed by a profile vault yet; do not collect real personal data or secrets in this pilot.

PostgreSQL tests use synthetic adapters to cover competing claims, per-user exclusion, isolation,
approval binding/expiry, revoked workers, crash recovery, payment handoff and reconciliation.
The next end-to-end milestone needs the Chrome extension, one tested site adapter and user UI.
It must verify local takeover, secret-free observation, reconnection, approval changes and receipt
capture before live chores are enabled. The wider multi-app roadmap remains deferred.

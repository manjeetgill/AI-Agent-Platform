# Request → saved agent → durable execution

This increment connects an application's natural-language request to a reusable, immutable,
version-pinned tool plan and the durable task worker. It is a bounded workflow agent: steps have
independent typed arguments. It is not yet an adaptive reasoning loop, arbitrary code generator,
or a browser operator. The included tool `decision.compare` computes a weighted ranking from
user-supplied scores; it does not invent facts or take action on the ranking.

## Deploy the pilot

1. Stop old workers and apply migration 009 with `python -m agent_platform.migrate`.
2. Set `AI_DURABLE_TASKS=1` and `AI_TASK_AGENTS=1` in the API environment. Browser tasks can remain off.
3. Configure the existing NVIDIA model gateway to enable natural-language interpretation.
4. Start API normally and the durable worker separately: `python -m agent_platform.task_worker`.
5. Provision an app key with `agents:read`, `agents:write`, `agents:run`, `tasks:read` and `tasks:write`.
   Grant `tasks:approve` only to an integration responsible for collecting explicit human approval.
6. Explicitly permit the tool using the operator CLI:

```sh
python -m agent_platform.service_identity grant-tool <app-id> decision.compare
```

Use `revoke-tool <app-id> decision.compare` to revoke it. New applications have no tool grants.
Migration 009 also attaches older service-user tasks to their applications. Those tasks now need
explicit tool grants too. Provision grants during rollout before restarting workers.

The app and worker must install matching adapter versions. The default registry contains only
`decision.compare`; no merchant, messaging, payment or browser adapter is bundled. Custom adapters
must declare a description, Pydantic input and output models, version, risk and bounded timeout.
Their code is trusted and operator-installed. Model output cannot register a tool or change risk.

## Application flow

Send the service authorization and authenticated external-user headers described in
[SERVICE_API.md](SERVICE_API.md). All endpoints are documented by runtime `/openapi.json`.

1. `GET /v1/task-agents/tools` returns only granted typed tools, including input/output schemas.
2. `POST /v1/task-agents/interpret` with `{ "request": "..." }` returns either:
   - `ready: false`, missing-information questions, and `blueprint: null`; or
   - `ready: true`, validated `blueprint`, and preview summaries, risks and action hashes.
3. Show the draft to the user. For missing information, combine the original request and their
   answers and interpret again. For unsupported work, explain which tool is missing. The planner
   is instructed not to invent inputs, but its factual extraction still needs user review.
4. `POST /v1/task-agents` with `{blueprint, idempotency_key}` validates again and saves the agent.
   Saving does not execute anything. Repeat keys with changed definitions return 409.
5. `POST /v1/task-agents/{id}/runs` with `{idempotency_key}` invokes saved arguments. Optionally
   supply `arguments: [objectForStep0, objectForStep1, ...]` to reuse the plan with new inputs.
   Each object must satisfy that step's tool input schema. Tool order and versions cannot change.
6. Invocation inserts task, exact steps, audit event and initial queue/waiting state in one
   transaction. It returns HTTP 202 with a task ID. Repeated identical invocation keys return
   the same task, including after completion; changed inputs return 409. Only one active task
   per app-bound user is allowed. Agent revisions are immutable; save a new agent to change a plan.
7. Use `/v1/tasks/{id}` or its event stream to show progress and per-step results. READ runs in
   the worker; SEND waits for an exact-action human yes; USER_ONLY waits for the user and verified
   reconciliation. Existing pause/resume/cancel and uncertain-outcome rules apply.
8. Use `GET /v1/task-agents` (paginated) and `GET /v1/task-agents/{id}` to retrieve saved agents.

Tool grants are checked during draft validation, saving, invocation and dispatch. Adapters must
recheck the supplied `authorized()` immediately before acting. A revoked queued tool pauses the
task and discards unused approvals. After regranting, resume and obtain fresh approval if required.
Read-only reconciliation stays available to resolve an already uncertain external action.

Each output is validated against the installed adapter's output schema before persistence.
Results live on task steps; there is no model-written aggregate result schema yet. A changed tool
version requires a new reviewed agent definition. No output-to-input references, branching or
model-generated executable code are accepted in this version.

## Try the decision example

[examples/task_agent_flow.py](../examples/task_agent_flow.py) makes the real HTTP calls, saves and
invokes a read-only agent, and observes its event stream. It requires the API, worker, configured
model, granted tool, and three environment variables: `AGENT_API_URL`, `AGENT_SERVICE_KEY`, and
`AGENT_EXTERNAL_USER_ID`. It stops on missing information or any non-read-only proposal. In a real
application, persist idempotency keys before requests and handle uncertain HTTP outcomes by reading
state. The example does not implement a production consent UI or restartable client session.

For the supplied example: cost weight 2, quality weight 1; A=(9,6), B=(6,9). The verified tool result
is A=8 and B=7. Reusing the agent with weights (1,2) reverses the ranking. These are calculations
from supplied preferences, not independently verified recommendations.

## Verification and remaining work

Database tests exercise HTTP interpretation with a simulated model, immutable save, invocation,
worker execution, result retrieval, reuse, schema/version rejection, scopes and owner isolation,
revoked grants, exact-action approval, concurrent deduplication and transaction rollback.
Live model reliability and an actual app/channel UI still need an integration pilot.

Interpretation is a bounded synchronous request (40 seconds), not a durable planning job. Saved
agents and invoked tasks persist; an interrupted unsaved draft must be requested again. Atomic
planning limits are 50 calls per app-bound user and 500 per app per database day; failed requests
count too. These are request limits, not monetary budgets or complete per-attempt token billing.
Full cost accounting, per-user tool grants, adaptive planning, dependent steps, profile vault,
standalone end-user login, browser integration, webhooks and cross-channel adapters remain ahead.

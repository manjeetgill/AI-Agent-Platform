# Research Lab durable task integration

Research Lab now calls `/v1/research-tasks/{task-agents|tasks}/...` on NRAIAlgo. Its authenticated
facade derives the external user from the session, supplies a server-only scoped service key,
and forwards typed requests to the platform. The browser cannot choose another user or supply
a service credential. The route is covered by NRAIAlgo's AI/scanner entitlements.

The dedicated app receives only `research.explain`. Its key has agents read/write/run and tasks
read/write, and **no task approval scope**. Financial tools are not granted. Worker dispatch
checks the app grant again. The adapter pins the configured NRAIAlgo app ID and maps its durable
owner back to the authenticated external user before fetching source evidence. Source ownership
and current account access are checked by the NRAIAlgo evidence connector.

## User flow

- Prepare or reopen a saved evidence snapshot.
- Select a compatible research agent, optionally an independent second reviewer or research goal.
- Run research. Optional natural-language planning uses the platform's typed planner; missing inputs
  are displayed. The client rejects drafts that substitute a different snapshot or incompatible agent.
- Save/reuse versioned plans, see tasks and audit history, pause/resume/cancel, and open verified reports.
- SSE updates task progress. Reconnection reads state; it never resubmits a run. Paginated task and
  saved-agent histories are available. Request digests and idempotency keys survive a page reload
  in session storage without storing evidence there.

Ordinary research, bounded cost calculations and monitoring have no extra confirmation prompts.
Research explains financial evidence but cannot transact. Payments, purchases, transfers and order
placement remain outside this integration and must retain explicit approval if introduced later.
The UI discloses that evidence and saved preferences are sent to NVIDIA.

`research.explain` is a READ action from the user's perspective, but it makes a provider call.
It persists an `agent_runs` dispatch marker **before** contacting NVIDIA. Worker retries check that
record: a completed report is reused; a started/uncertain call is never sent again. Reports and token
usage stay in the existing Agent Runs history, and saved research preferences/observations use the
existing memory store. A cancelled task cannot undo a provider request already sent. A completed
provider response may still be saved in Agent Runs even if the task has been cancelled.

## Local rollout

Use the actual research database, apply migrations, then run:

```
.venv/bin/python scripts/connect_research.py \
  --consumer-config /absolute/path/to/NRAIAlgo/.runtime/agent-client.json \
  --connector-url http://127.0.0.1:4062
.venv/bin/python scripts/local.py start
```

The setup command preserves existing configuration, pins the app, grants only the research tool,
and writes private credentials without printing them. It is a loopback development helper.
Restart the NRAIAlgo API to load the key. Keys expire after 90 days; rerun setup to rotate while
preserving app/user identities. Revoke old keys using the service identity administration command.

For deployment, set `AI_DURABLE_TASKS=1`, `AI_TASK_AGENTS=1`, `AI_PLATFORM_RESEARCH_APP_ID` and the
connector/model/database settings on the platform API **and worker**. Set
`AI_PLATFORM_RESEARCH_SERVICE_KEY` on NRAIAlgo only. Compose now includes a separate worker.
The local launcher starts/stops its worker alongside the API. Monitor worker process health in deployment.

## Limits

- Only Research Lab uses this new execution path; older Agent Runs and Workflows entry points retain
  their existing execution paths. Monitors and bounded cost calculations remain in NRAIAlgo.
- Each tool attempt is bounded to 30 seconds within a 45-second worker lease. An uncertain model
  timeout ends in a failed task after local status checks, without repeating the external call.
  No automatic provider refund, complete failed-attempt usage accounting or monetary budget is implied.
- Review steps independently explain the same evidence; this is not a debate or an adaptive agent loop.
- Browser chores, payment tools, arbitrary remote tools and agent sharing are not enabled here.
- An unsaved natural-language draft is not durable. Saved definitions, invoked tasks and reports are.

Verification covers task/agent HTTP contracts, owner isolation, service-key header replacement,
endpoint allowlists, changed/disabled agents, revoked grants, provider timeout deduplication,
UI idempotent retries and rejection of drafts that switch evidence.

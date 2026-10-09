# External jobs and result-dependent workflows

Apply migration `040_external_jobs.sql` before running the updated API or worker.
The platform uses its existing PostgreSQL task queue; there is no second broker.
`AI_AUTOMATIONS=1` installs `runtime.workflow` (version 1). An operator must grant
that tool **and every domain tool** to the consuming application.

## Waiting for domain work

An operator-installed adapter can implement:

```python
from agent_platform.external_jobs import DeferredJob, JobFailure

# execute receives a stable operation_id for adapters that implement poll.
# Use it as the domain service's idempotency key before creating any work.
async def execute(self, action, *, operation_id, authorized, **context):
    if not await authorized():
        raise TaskError('Execution authority revoked')
    job = await domain.create_job(action.arguments, idempotency_key=operation_id)
    return DeferredJob({'job_id': job.id}, poll_after_seconds=10, timeout_seconds=900)

# poll must only read the admitted job. It must NEVER resubmit collection work.
async def poll(self, action, *, reference, authorized, **context):
    if not await authorized():
        raise TaskError('Execution authority revoked')
    job = await domain.read_job(reference['job_id'])
    if job.failed:
        return JobFailure({'reason': job.reason_code})
    if not job.complete:
        return DeferredJob(reference, poll_after_seconds=10)
    return self.output_model.model_validate(job.result).model_dump()
```

This snippet defines a contract; `domain` is the consuming application's client,
not a new platform HTTP endpoint. The adapter owns destination allowlisting,
credentials, domain status validation and redaction. References must be non-secret
IDs, never access tokens, credential-bearing URLs or untrusted network destinations.

The admission call retains its declared risk (`SEND` for collection requests that
change a queue), exact action hash and approval. Once the adapter confirms a job ID,
the worker saves the reference, original deadline and a future `POLL` queue entry in
one transaction. The worker is then free to serve another user. The existing
one-active-task-per-owner constraint still applies while that owner's job waits.

Completion is only recorded after the final result passes the tool's output schema.
The API's task status stays `READY` between polls for compatibility. Inspect the
step's `external_job_state`, deadline, poll count and events for waiting progress.
Tool discovery advertises `asynchronous: true` for adapters with a poll method.

### Failures, restart and controls

- Polling is bounded by the original deadline (maximum one hour) and 120 polls.
  Returning another pending acknowledgement never extends the deadline.
- Three consecutive failed polls stop tracking with `OUTCOME_UNKNOWN` and
  `UNAVAILABLE`. Deadline/poll-budget exhaustion uses `TIMED_OUT`. Neither path
  resubmits the original request or advances the workflow.
- A definite domain `JobFailure` marks the task `FAILED` and saves its evidence.
- A crashed polling worker resumes **polling** under a new lease, including for a
  tool originally classified `SEND`.
- A lost admission response remains `OUTCOME_UNKNOWN`. An explicit reconcile uses
  the same `operation_id`; the adapter must check the domain's real state. It can
  return an acknowledged `DeferredJob` or a verified final `Reconciliation`.
  No automatic duplicate submission is permitted.
- Pause stops local polling; resume continues the same job. Cancel stops the local
  workflow and records `DETACHED`. **It does not claim to cancel the external job.**
  Remote cancellation needs its own separately authorized domain tool.
- Grants, agent lifecycle and automation approval are checked again before polling.
  References and results remain behind the task's existing app/owner authorization.

## Passing results between steps

Register a normal task agent whose sole initial step is `runtime.workflow`. Its
arguments contain up to eight version-pinned domain steps. Nested runtime tools
are rejected. Literal values go in `arguments`; `bindings` fill distinct top-level
argument names from earlier completed domain steps using JSON pointers.

```json
{
  "tool": "runtime.workflow",
  "version": "1",
  "arguments": {
    "steps": [
      {
        "tool": "example.collect",
        "version": "1",
        "arguments": {"source": "explicit-user-supplied-source"}
      },
      {
        "tool": "example.evaluate",
        "version": "1",
        "arguments": {"threshold": 60000},
        "bindings": {
          "product_id": {"step": 0, "pointer": "/product_id"},
          "observed_price": {"step": 0, "pointer": "/price"}
        }
      }
    ]
  }
}
```

`example.*` names illustrate domain adapters; they are not installed tools. Step
indexes refer to domain steps, excluding the coordinator's internal checkpoints.
Pointers may select dictionary fields or array indexes; no code, expressions,
interpolation, searches or arbitrary HTTP calls are evaluated. Missing values,
forward references, changed versions, invalid resolved inputs or revoked grants
stop execution. A resolved SEND action pauses for approval of its **exact** values;
approving an earlier step cannot approve an unknown future price or recipient.

A workflow uses `2 × domain steps + 1` durable steps. The app's `max_plan_steps`
limit is checked before the first domain action. All completed domain results stay
in the ordinary task record. For automation conditions use `steps.*.<field>` where
appropriate; the final coordinator result is a completion summary, not a price.

## DealAgent connection boundary

Audited `dealagent.collection_request` v1 calls `/v1/crawler/enqueue`, which returns
inserted/existing counts. It has no durable per-request job ID and no immutable
terminal result. It continues to report **queue acknowledgement only**. This release
does not reinterpret that acknowledgement as successful price collection.

A later DealAgent adapter should implement the contract above using a domain job
record with: a stable operation ID, accepted canonical URLs, location, per-URL
status/failure reason, terminal state, and validated observation IDs/timestamps.
The domain service must persist idempotency before accepting work and enforce its
own retailer concurrency, freshness, robots/access policy and request limits.
Only validated observations may enter subsequent comparison steps. Catalogue
prices, retailer scraping, fees and deal calculations remain domain-owned.

## Verification

Database-backed tests exercise HTTP agent creation → approval → admission → pending
poll → worker restart → completion → result binding → exact SEND approval. Additional
checks cover cross-owner denial, lost acknowledgement reconciliation, missing result
fields, explicit remote failure, timeout, repeated poll errors, pause/resume/cancel,
revoked grants and total step budgets. These are controlled adapter fixtures; they
are not evidence that a live retailer collection was performed.

# Platform roadmap

## Implemented foundation

- Transactional PostgreSQL task queue, claims, leases, read retry limits and exact-action approval.
- Durable uncertainty across pause/cancel/reconciliation and worker recovery.
- Organisations, applications, scoped expiring/revocable service keys, app-bound external users.
- Task creation idempotency, owner-scoped keyset pagination, replayable event API and SSE.
- Operator-installed typed action adapters, mutation audit principals, versioned task routes.

See [service API](SERVICE_API.md) for rollout and integration. This foundation is tested with
synthetic adapters and is not a production browser agent.

## Next: execution policy and accounting

Persist per-app and per-user tool grants, atomic quota/cost reservations, usage records per model
attempt and tool dispatch, deadlines, cancellation and bounded retries. Release reservations or
settle actual usage transactionally. Test simultaneous calls at quota boundaries and provider
failure after consumption. Uncertain submission must remain unresolved even when a budget expires.

## Next: browser pilot

Build Chrome device pairing, authenticated command transport, local takeover, stale-token rejection,
secret-free observation, allowed-site enforcement and a single tested adapter. Add plan/approval/
receipt UI. Run crash, disconnect, site-change and payment-handoff scenarios against a controlled
fixture before enabling a live site. Payments, passwords and OTP entry remain user-only.

## Then: reusable agents and model gateway

Add typed template inputs/outputs, version-pinned invocations, user/org/global sharing with explicit
publication permission, bounded planner/tool loop, provider routing and failover, streaming model
output and complete usage accounting. Test changes to site adapters and template schemas.

## Then: external integration and scaling

Add Python and TypeScript SDKs, independently authenticated end-user approvals, OAuth where needed,
Postgres RLS with a separate restricted API role, durable signed webhook delivery with deduplication,
inbound trigger authentication, tool-provider credentials and isolation, and webhook/MCP tool
registration with per-tool policy. Separate importable orchestration core from HTTP adapters.

Before broad release: load-test active streams and worker contention, add trace/metrics dashboards,
alerting, retention/deletion policies, key rotation operations, backup/restore drills and contract
tests. Export stable API snapshots and define a compatibility/deprecation policy.

## Explicitly unfinished

No production browser extension, merchant/payment integration, profile vault, adaptive agent loop,
per-app monetary budgets, usage billing, registered remote tools, webhook delivery, standalone user
login or general multi-app access to legacy research endpoints is supplied by this increment.

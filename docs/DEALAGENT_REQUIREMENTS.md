# DealAgent requirements derived from source

Audit date: 2026-10-08. Consumer: `/Users/manjeet/Desktop/dealagent/dealagent`.
This is an implementation ledger, not a production certification.

| Consumer evidence / requirement | Existing platform | Gap / implementation | Verification |
| --- | --- | --- | --- |
| `src/lib/agents/shopping.ts` calls local Ollama and domain search | Structured gateway, fallback, typed task agents | Route the connected consumer through interpret/save/run/result APIs | Consumer HTTP contract and real model/worker run |
| `src/lib/watches.ts` stores single-owner JSON | Durable PostgreSQL tasks | Generic approved automations, time/event triggers, persistent state | Restart, idempotency and concurrent dispatch tests |
| `engine/src/engine.ts` owns collector loop and demand tiers | Task worker leases/recovery | Keep acquisition mechanics in engine; platform owns user monitoring schedules | Scheduled read task → condition → notification |
| `engine/src/server.ts` exposes observation/history/catalogue | Three installed domain tools | Expose typed observation/history tool with source IDs and location | Adapter fixture and live observation tests |
| Watch target/channel/city are user data | Reviewed memory + app identity | Versioned structured preferences API, owner isolation | Cross-user and cross-app tests |
| `src/platform/orchestrator.ts`, `src/domain/pricing.ts` compare/score | Generic tool registry | Retain deterministic domain calculations; no model-generated prices | Existing shopping tests |
| Retailer health, CAPTCHA, blocked and stale rows | Coverage tool | Preserve unknowns, report operational failures | Coverage contract |
| Monitoring results need deduplication | Task audit events | Versioned durable events, event subscriptions, in-app inbox | Event replay/deduplication and threshold crossing |
| Future consumers need lifecycle/configuration | Immutable blueprints with pinned tools | Versioned manifest plus DRAFT/ACTIVE/PAUSED/DISABLED/ARCHIVED | Lifecycle dispatch/revocation tests |
| Tenant boundaries | App service keys, owner IDs, tool grants | Composite ownership keys and forced RLS for new orchestration tables | Non-superuser database isolation test |
| Trial limits | Daily planning quotas | Daily automation run cap, schedule count, bounded event/notification payloads | Limit exhaustion and denied dispatch |
| Long work / uncertain submission | Durable task leases, READ retry, SEND reconciliation | Reuse existing queue; one active user task, backpressure | Existing durable-task contracts |
| Secrets | Operator plugin environment; encrypted channel credentials | Keep references out of prompts; no shared DB keys | Public-response secret checks |
| Context / memory | Candidate review, handoffs, embeddings, tenant retrieval | Include bounded explicit preferences in planning | Prompt isolation contract |
| Notifications | WhatsApp app events and signed channel callbacks | Add provider-neutral in-app notification envelope first | Atomic event/inbox transaction |

Public deployment gates remain separate: authenticated DealAgent end-user sessions,
non-superuser deployment DB roles, production secret storage, stable HTTPS, provider
agreements, backup/restore and load testing. The current consumer has no general
authenticated user/session layer; its local owner mapping must never be presented
as public multi-user authentication.

The generic runtime now supports validated fixed plans and a bounded `runtime.next`
model/tool/result loop using the same grants, leases and approval gates. Signed outbound
webhooks have persisted delivery attempts and replay protection. Streaming model output,
monetary billing and multi-provider cost reconciliation remain outside this release;
unreported cost is returned as unknown, never zero.

## Local verification

The connected app at `http://localhost:3301` successfully executed the real-model request
“Find Samsung TVs under ₹60,000.” through task `50f33a0e-5a31-40e3-8f83-2aa3b2c9398d`.
Three stored retailer offers were returned with links, observation timestamps and unknown
fees. This verifies catalogue observations, not current checkout prices or fresh retailer fetches.
Monitor `c1b5cc8e-460f-4934-badf-81ec27192d9d` required exact approval, ran through the
scheduler/worker, created an in-app notification, and was archived after the test.

The additional live adaptive-loop task `5fb7859f-b997-428a-9d55-640fbaba79b1`
failed because the configured model endpoint returned HTTP 503. Its worker stopped
at the existing three-attempt bound and did not invoke a domain tool. The adaptive
loop's continuation, durable resume, denied-tool and exact SEND approval contracts
pass against the isolated database with a deterministic model fixture; live provider
verification for that additional mode remains outstanding.

Verification commands:

- `.venv/bin/python scripts/test_orchestration_local.py`: 41 database/runtime tests.
- `.venv/bin/python -m pytest -q tests/python/test_nvidia_gateway.py`: 11 gateway tests.
- DealAgent `npm test -- tests/platform-execution.test.ts tests/agent-platform.test.ts tests/shopping-agent.test.ts tests/watches.test.ts`: 19 consumer tests.
- DealAgent tool package: 5 adapter tests; TypeScript checking passed.
- Local `/assistant` and `/monitoring` pages returned HTTP 200.

No public deployment, GitHub push, paid subscription, purchase, or external message
was performed for this integration. Credentials remain in private local configuration.

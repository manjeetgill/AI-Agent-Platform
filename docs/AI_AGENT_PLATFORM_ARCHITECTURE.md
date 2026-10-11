# AI Agent Platform architecture

Code review baseline: 9 October 2026, GitHub main c1b9a6a, plus local security fixes.

## Ownership and runtime

This independent Python/FastAPI service owns agent definitions, durable task state,
approvals, memory, channel bindings and usage records in PostgreSQL. Consuming apps
own users, business records and domain tools. The Agent Workspace is a local
operator console, not a hosted multi-user customer dashboard.

The durable queue is PostgreSQL's `chore_jobs` table. Queue and task changes commit
together; workers use row locks and leases. This repository does not require Redis
and does not implement a separate Agent Factory service.

## Implemented and remaining

| Capability | Implemented | Configuration or planned work |
| --- | --- | --- |
| App identity | Scoped service keys, app-owned users, forced RLS and separate API/worker roles | Apply validated role/RLS migration to each deployment |
| Planning | Typed blueprints, tool grants, model-assisted plans, app quotas | Provider access and app grants |
| Execution | Durable queue, leases, exact-action approvals, bounded retries and reconciliation | Domain adapters and real-effect verification |
| Adaptive agents | Bounded model/tool loop | Not unrestricted autonomous execution |
| Scheduling | PostgreSQL triggers, conditions, notifications and durable deliveries | Approved destinations and source adapters |
| Memory/RAG | Structured checkpoints, reviewed memories, lexical retrieval, optional local embeddings | Documents, retention, consent and local embedding assets |
| Models | Schema/evidence validation; explicit ordered provider chain, PostgreSQL budget reservations, cooldowns and sensitivity policies | Configure keys, verified rates and per-app policies; run a bounded live pilot |
| WhatsApp | Meta/Chakra connections, signed callbacks, message queues and clinic bridge | Approved business/number, credentials, callback and live tests |
| Voice | Internet voice, WhatsApp calling and Asterisk/BYOC adapters | Optional media runtime, authorized number/carrier and audio/network tests |
| Email | Mock, Resend, Brevo, SMTP, limits, retries and delivery events | Verified sender and callbacks; SMTP acceptance is not delivery |
| Retailer evidence | Paired Chrome runner, bounded server renderer, feeds and evidence checks | Approved source access and working adapters |
| Usage | Migration-039 model-call records and scoped usage export | Currency is not recorded; estimates are not provider invoices |
| Fine-tuning | Synthetic local pilot and reviewed-example export | No automatic production training or deployed fine-tuned model |

## Request flow

1. A consuming backend uses its own service key and authenticated external user ID.
   NRAIAlgo research supports app-bound Ed25519 delegations; legacy HMAC remains during explicit migration overlap.
2. The platform checks scope, app grants, ownership and quotas, then saves a typed plan.
3. SEND steps retain the existing exact-action approval. USER_ONLY actions are not executed.
4. Workers save progress and reconcile uncertain effects before repeating work.
5. The consuming app reads results/events or receives configured webhooks. Task and
   memory state remains outside the LLM when providers change.

## Security review status

The local update disables HTTP OpenAPI except in explicit development mode,
authenticates versioned APIs before body validation, avoids identity INSERTs for
returning users, and checks the complete route inventory with optional flags enabled.
Public callbacks and expiring connection/voice sessions retain their own verification.

Migration-054 inventory: 109 tables; all 103 tenant/app tables have forced RLS.
Six global/operator tables are explicitly classified. API scope is transaction-local;
worker access requires a restricted worker role, not just a caller-set session flag.
Public callbacks bootstrap only their bound identity and still verify their signature.
See `security/table-inventory.json` and `security/ROLLOUT.md`.

Delegation supports app-bound Ed25519 issuer keys, validity windows, replay rejection
and legacy overlap. Existing approval behavior is unchanged by owner choice.
Legacy retirement and live database cutover are operational steps, not implied by
source-code completion. The local operator console deliberately has a separate
worker credential; public deployment must never receive that operator credential.

## Sahayak contract

`src/agent_platform/sahayak_bridge.py` and Sahayak's
`apps/api/src/whatsapp-bridge.ts` agree on event, channel, app and clinic identifiers,
sender and text. Both use HMAC-SHA256 over timestamp, a dot and the exact JSON bytes,
with X-Agent-Timestamp and X-Agent-Signature headers. Sahayak enforces a five-minute
time window and app/clinic binding. Probe responses contain the same clinic ID;
normal responses contain the event ID, replies, handledBy and redFlag. This is a
code-contract review, not a new live appointment/message-delivery test.

## Deployment

The platform has its own containers and database. The guarded release script backs
up data, checks migration checksums, preserves private settings and verifies HTTPS
boundaries. This security review is local work; it does not change the live server.
Provider activation and customer authorization remain separate setup steps.

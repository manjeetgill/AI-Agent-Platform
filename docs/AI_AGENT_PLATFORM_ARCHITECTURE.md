# AI Agent Platform architecture

## Original consumer audit (2026-10-03)

NRAIAlgo is a Next.js frontend (`frontend/nextjs`) and FastAPI backend (`backend/python/nraialgo`) backed by PostgreSQL numbered migrations. Browser authentication is a server-side session cookie; `api.auth.identity` validates session age, MFA and edition membership. The technical scanner writes workspace-scoped manual runs (`technical_runs`, `technical_run_items`) and public scheduled observations (`technical_scans`). Existing AI code in `nraialgo.ai` already provides NVIDIA/Groq/Gemini transports and bounded public-data reviews. The app has scanner, backtest, broker, execution, notification and news services, with the broker and execution paths outside this platform's boundary. The frontend proxies `/v1/*` to FastAPI. Tests use pytest and Vitest. Deployment is configured under `deploy/`; migrations are explicit and checksum-verified. Existing AI reviews do not provide agent definitions, tools or private research runs.

## Boundary and ownership

The platform now lives in this independent repository at `src/agent_platform` as an independently startable FastAPI service with its own database and credentials. It never imports the NRAIAlgo store, broker or execution modules. NRAIAlgo owns identity, scanner facts and access checks; the platform owns agent definitions, run state, reports, preferences, observations and quotas. Communication is HTTP through fixed, allowlisted connectors. All tools are read-only. No order or broker tool is registered.

## Request and execution flow

1. The signed-in browser uses NRAIAlgo's `/v1/agents/*` facade. NRAIAlgo validates the existing session and issues a short-lived HMAC-signed delegation for the platform with issuer, audience, tenant, user, scope, expiry and unique request ID.
2. The platform validates the delegation and checks agent ownership, tool permission and run limits in its own database. A unique idempotency key prevents duplicate logical runs.
3. For scanner research, the platform signs a separate audience-restricted tool token containing tenant, user, agent, run, tool and scope. NRAIAlgo independently validates it and fetches the requested manual scan with `workspace_id = user_id`.
4. The platform validates and bounds the evidence, asks the configured NVIDIA model for structured commentary, validates source references against the retrieved evidence, and persists the report with immutable agent version. Backtest and completed options paper trade connectors provide separate read-only evidence scopes.
5. The browser reads the run through the facade. A provider or connector outage yields a failed run and does not affect non-AI NRAIAlgo routes.

## Security and failure model

Shared signing keys are supplied only through environment secrets; production must use distinct signing and provider keys. Delegations are valid for at most one minute, audience restricted and scoped. Tool data is serialized as untrusted data in the model prompt. Tool names and permissions are selected by server code, never by model output. All platform queries include tenant and user ownership predicates. The model cannot write facts, trade, call a broker or add capabilities. Execution tiers 4 and 5 are unavailable. Timeouts bound connector and provider calls. Run and request IDs permit correlation without logging prompts, tokens or private data.

## Deployment and migration

Deploy the platform API separately using its own Dockerfile and Compose project. Configure platform settings from `.env.example`; configure the NRAIAlgo facade separately in its repository. Migrate its independent database before starting it. Enable the NRAIAlgo facade after the platform health check succeeds. Rollback by disabling the feature flag; existing app routes and data remain intact. The platform schema is additive and isolated. Do not share NRAIAlgo database credentials with the platform.

## Open work

The current implementation supports five built-ins over saved scanner, archive backtest and completed options paper trade evidence. Trade Review covers simulated paper results only. User preferences, model-labeled observations, run history and text search are separate stores; trading facts stay in NRAIAlgo. Two bounded, optional workflows run Signal Research → Signal Challenger or Backtest Auditor → Strategy Review, with persisted step runs and idempotency. The strategy builder interprets only the existing archive DSL, validates a structured draft, shows it for review, and submits to the existing backtest API only after explicit confirmation. Scheduling, broader trading-data retrieval, notifications, and production load validation must be completed before declaring the full platform production ready.

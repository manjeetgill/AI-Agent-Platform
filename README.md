# AI Agent Platform

Independent Python service extracted from NRAIAlgo on 2026-10-04. This project owns the runtime, model gateway, agent definitions, workflows, memory, PostgreSQL migrations, evaluation fixtures and service deployment. It has no Python, build or database dependency on the NRAIAlgo repository.

NRAIAlgo is the first consumer. Its application UI, login/session validation, HTTP facade and evidence connector stay in its own repository. The only shared boundary is the versioned HTTP/delegation contract in `docs/INTEGRATION_PROTOCOL.md`. The platform still includes NRAIAlgo-oriented research agents and tools; extraction does not make their contracts generic.

## Reuse across apps and businesses

The generic task API supports app-specific built-in catalogues, private user agents, configurable
planning limits and explicitly enabled business tool packages. New domains supply adapters and
versioned templates without editing the engine. See [multi-app onboarding](docs/MULTI_APP_PLATFORM.md)
and the separate [shipping tool example](examples/shipping_plugin/pyproject.toml).
Legacy NRAIAlgo research endpoints remain app-specific; use the generic task API for new apps.

## Persistent orchestration

External tools can now return durable job acknowledgements and resume through
bounded, read-only polling. `runtime.workflow` binds verified earlier results into
version-pinned later actions, preserving exact SEND approvals. See
[asynchronous jobs and workflows](docs/ASYNC_JOBS_AND_WORKFLOWS.md).

Connector verification uses granted read-only adapters, persistent evidence reports,
per-source cooldowns and app quotas through the same task worker and scheduler.
See [connector checks](docs/CONNECTOR_CHECKS.md).

[API/feed connections](docs/API_FEED_CONNECTIONS.md) add approved JSON feed profiles,
encrypted API credentials, queued setup tests, bounded pagination/retries and a
local setup screen at `/connectors`, including recovery instructions and bounded
schedule review/activation. Approved alternative feeds are matched by application,
tool and source. Reports distinguish unspecified, stored-observation and live-source
verification; retailer live coverage still depends on the installed adapters.
The optional [DealAgent live probe](docs/DEALAGENT_LIVE_CHECKS.md) has now checked
the original eight remaining sources and recorded their actual access outcomes.
[Reviewed browser evidence](docs/REVIEWED_BROWSER_EVIDENCE.md) provides a scoped
manual fallback with immutable capture dates, location/variant warnings and
explicit stored-observation provenance. It does not claim unattended collection.

`AI_AUTOMATIONS=1` enables versioned agent manifests, lifecycle controls, approved
time/event schedules, bounded conditions, structured preferences, model usage records,
an in-app inbox and signed callbacks. The existing PostgreSQL task worker owns
dispatch and recovery. See [orchestration API](docs/ORCHESTRATION_API.md) and the
[DealAgent requirements audit](docs/DEALAGENT_REQUIREMENTS.md). Public deployment
still needs consumer authentication and deployment verification.

## Knowledge and RAG

Apps can ingest private or app-wide text documents, search permitted passages and generate answers
with validated source references. The same capability is available as the `knowledge.answer` agent
tool. See [RAG integration](docs/KNOWLEDGE_RAG.md) and the [API example](examples/knowledge_flow.py).
Retrieval currently uses PostgreSQL English full-text search; embeddings and binary document
extraction are not included. Knowledge access is explicitly scoped per app.

## Prepared local setup (macOS with PostgreSQL 17)

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/local.py setup
.venv/bin/python scripts/local.py start
```

Setup generates a private signing key and database password once. The launcher initializes its own PostgreSQL cluster on 55460, applies platform migrations and serves the API on 4060. It preserves reports and settings across restarts. Configuration is stored under ignored `.runtime/local/` with permissions 0600. It does not use the NRAIAlgo database.

The optional [local task monitor](http://127.0.0.1:4060/monitor) shows tasks,
results, readiness, recovery and usage for explicitly configured app connections.
See [private monitor setup](docs/ORCHESTRATION_API.md#local-task-monitor).
The [local app onboarding wizard](http://127.0.0.1:4060/monitor/onboard) creates
scoped app connections with private credentials and a provider-free worker test;
[enable and use onboarding](docs/ORCHESTRATION_API.md#local-application-onboarding-and-connection-tests).

When ready to configure NVIDIA, use your own Terminal:

```sh
.venv/bin/python scripts/local.py nvidia
```

Enter the key privately, then an accessible model ID. The optional reasoning model defaults to the same ID. Restart `scripts/local.py start` afterward. This command saves settings; a successful agent run verifies actual provider access. Before configuration, the API, agent catalog, custom-agent saving and history work; model actions show a setup message in the consuming UI.

The consumer imports only `.runtime/local/client.json` during connection setup. This file contains the platform URL and signing secret, never the NVIDIA key or platform database password. For NRAIAlgo, run `npm run local:agents:connect` in its repository. The connector URL defaults to its local API at 4022.

```sh
.venv/bin/python scripts/local.py status
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts/local.py
```

Ctrl-C stops the API and retains the database. `scripts/local.py stop-db` stops only this project's local database. Docker deployment remains available through `compose.yaml` and `.env.example`; do not run it on the same API port while the native launcher is running. See `docs/AI_AGENT_DEPLOYMENT.md`.

PostgreSQL tests require `AGENT_TEST_DATABASE_URL` pointing to a disposable migrated database. Unit tests require no NRAIAlgo checkout.

## Status

The [browser task revision](docs/BROWSER_TASK_REVISION.md) now includes a durable PostgreSQL
queue, exact-action approvals, pause/cancel fencing, and reconciliation of uncertain outcomes.
Its task API is disabled by default. Chrome control, site adapters, the profile vault and the
task UI are not implemented yet; all execution tests use synthetic adapters.

This preserves the existing partial implementation. Scheduled triggers, broader trading RAG,
production load tests and credentialed browser/provider verification remain unfinished. Durable
app event notifications are available through the WhatsApp integration, subject to sender and
recipient verification. No live browser execution tools are present. This extraction does not
declare production readiness.

The independent source project is connected to its own GitHub repository.

## Multi-app roadmap

The first service API increment adds scoped application credentials, app-bound users, pagination
and replayable task events. See [integration and rollout](docs/SERVICE_API.md) and the
[remaining roadmap](docs/ROADMAP.md). Browser execution remains disabled by default.

## Request-to-agent pilot

[Request → saved agent → durable execution](docs/TASK_AGENT_FLOW.md) now connects the multi-app API
using typed, explicitly granted tools. Includes a read-only decision comparison adapter and a
[runnable HTTP example](examples/task_agent_flow.py). Natural-language planning requires the
configured model; flags remain off until operator setup. No browser automation is bundled.

## Optional image analysis

With the existing NVIDIA key saved, run `.venv/bin/python scripts/local.py vision`,
then restart the service. This sets `AI_VISION=1` and
`AI_PLATFORM_NVIDIA_VISION_MODEL=nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` without
changing the text/reasoning models. Deployments must set those variables explicitly.

Authenticated consumers use `GET /v1/agents/vision` for configuration,
`POST /v1/agents/vision/analyze` with `{image, question, mode, consent:true}`, and
`GET /v1/agents/vision/reports` for their latest 20 reports. `image` is a base64 PNG
or JPEG data URL; `mode` is `chart` or `document`. Remote URLs, PDFs and animated
images are unsupported. Inputs are limited to 2,000,000 decoded bytes, 8 megapixels,
and a 3,000,000-byte request. The service re-encodes images without metadata and
resizes to fit 2048 pixels before sending to NVIDIA. Uploads are not persisted;
questions and generated reports are stored by tenant and user. Requests share the
existing atomic 20/day AI-builder allowance. Images and prompts must not be logged
by a deployment's reverse proxy. Reports contain model-extracted content, not
independently verified market facts. Migration 010 adds report storage.

### Combined research workflow reports

`GET /v1/agents/workflows/{id}` now includes `steps`, `completed_steps`,
`expected_steps`, `complete`, and coverage limitations. Each step is loaded with the
same tenant/user ownership predicates as individual reports. Failed or missing
reports remain explicit; no consensus, historical statistic or numerical score is
invented. The current workflows independently review one saved signal or one
backtest with two agents. Catalog entries expose a stable `builtin_key` for consumer
applications to present domain-specific names without changing saved agent IDs.

### Deterministic research evidence

The reusable `research_evidence` tool accepts only `snapshot_id`. Its consumer
connector retrieves a permission-scoped immutable evidence snapshot and rechecks
current permissions. Domain calculations and source adapters remain in NRAIAlgo.
A different consumer can implement the same signed connector endpoint.

`research-v1` evidence carries typed facts (`label`, `value`, `unit`, `basis`),
methodology/version/source coverage, limitations, freshness and a digest. Reports
use `numeric_claims` for exact copies of numerical measurements: label, unit and
value must all match a supplied measurement. Free prose is qualitative; digit
and common spelled-number prose statements are omitted with a visible note.
Incorrect typed numerical claims are rejected and repaired once. This validation
is a guard against numeric invention, not proof of every semantic model claim.
Evidence over the bounded size/count limit fails instead of silently dropping
research facts. Scanner evidence additionally supplies typed mean/median volume
measurements from the consumer.

Added research built-ins cover historical patterns, portfolio risk, market
context, multi-trade reviews, journal/behavior, strategy diagnosis/comparison,
options and approved robustness results. `trading_review` and
`strategy_comparison` workflows preserve separate step reports. Apply migration
`012_research_workflows.sql` before startup. NRAIAlgo owns approved experiment
plans and read-only monitoring/delivery consent; this platform does not acquire
broker execution privileges or autonomously run sensitivity searches.

### WhatsApp support agents

Opt-in inbound text support with document-scoped RAG, durable replies, conversation
history and human handover APIs. The local setup wizard and inbox are at
`http://127.0.0.1:4061/whatsapp` (start `scripts/console.py`). See [setup and limitations](docs/WHATSAPP_SUPPORT.md).

Consuming apps can bind an owned, verified sender and deliver provider-neutral event
notifications to recipients who prove number ownership in WhatsApp. See the
[multi-app notification contract](docs/WHATSAPP_APP_NOTIFICATIONS.md) and its
[backend client](examples/whatsapp-notifications/client.py).
They can render one isolated setup overview from the
[integration readiness endpoint](docs/APP_INTEGRATION_READINESS.md).

## Calling agents

WhatsApp calling is available through `/v1/calling`: app/owner-isolated agent setup,
existing Meta/Chakra business-number connections, permission checks, approved test
calls and an optional WebRTC audio worker. The local setup screen is `/calling`.
Calling is disabled until the number, audio runtime and business handler are ready;
the ordinary telephone connector has been removed. See [Calling integration](docs/CALLING_AGENTS.md) and the
[backend client example](examples/calling-integration/client.py).

## Internet voice pilot

Create an FAQ voice agent and talk in the browser at `http://127.0.0.1:4061/voice` after starting the local console. No phone number or WhatsApp connection is required. See [browser voice setup, API and app integration](docs/WEB_VOICE.md) for configuration, limits and public deployment requirements.

### App-owned telephone connections

Multiple apps can register isolated inbound calling connections using their
own authorized carrier/Asterisk gateways. Number verification, activation, scoped
gateway credentials, platform speech, app-provided booking tools, explicit confirmation, app-wide quotas and durable call turns are provided by
`/v1/telephony`. See [setup and API contract](docs/APP_OWNED_CALLING.md).
The operator console is `/telephony`. Actual telephone service requires the app's
carrier route and a successful live test; outbound calls are not enabled by this adapter.

## Validated agent memory

Provider-neutral memory now includes opt-in candidate capture, review, hybrid lexical/local semantic retrieval, task handoffs, outcome feedback, evaluation and consent-controlled dataset export. Open `/memory` in Agent Workspace. Apply migrations 030 and 031. See [Agent memory](docs/AGENT_MEMORY.md) and [backend app integration](examples/memory-integration/README.md). Actual model training is a separate reviewed operation; this workflow never starts a paid training job automatically.

### Retailer checks for mobile and web apps

See [Retailer browser runner](docs/RETAILER_BROWSER_RUNNER.md) for default server execution (`retailer.observe`), optional private Chrome pairing, worker recovery, and the remaining location/evidence limitations. End users do not need an extension for server checks. It is disabled unless `AI_RETAILER_BROWSER=1`; optional retailer access requires the user’s Chrome permission.

### Shared transactional email

The email phase adds an isolated application API, encrypted durable queue,
versioned templates, explicit mock/Resend/Brevo/SMTP adapters and app/global usage caps.
Start with mock mode; provider acceptance is never labelled inbox delivery.
Authenticated Resend/Brevo callbacks track delivery separately and suppress
permanent bounces, complaints and provider unsubscribe events within each app.
See [email setup and API](docs/ORCHESTRATION_API.md#transactional-email-phase-1).
Local read-only email activity is at `http://127.0.0.1:4060/monitor/email`.

### Unified Agent Workspace

Open `http://127.0.0.1:4061/` for the platform overview. Its shared navigation
includes the agent builder, WhatsApp inbox/setup, WhatsApp calling, internet voice,
telephone connections, memory/knowledge, API feeds, tasks/readiness, email and app
connections. The original request editor and saved agents are at `/workspace`.

The `/monitor` screens now open inside this workspace through a fixed loopback
bridge to the operations API on port 4060. Both services must run for app monitoring
and email. An operations outage shows an unavailable message while the other
workspace screens remain accessible. This unifies navigation; it does not merge
application identities, credentials or the console pilot database. Existing
connections keep their scopes. No provider is activated by opening a capability.

### Self-managed services

The workspace's `/self-hosting` page shows offline speech and own-SMTP setup
status, plus the existing direct Meta connection path. Apps keep the same API
contracts. Offline speech uses operator-installed whisper.cpp and eSpeak NG;
SMTP uses an operator-owned relay with TLS, durable queue limits and conservative
handling of uncertain sends. No route is automatically switched or activated.
See [self-hosted service setup](docs/SELF_HOSTED_SERVICES.md) for configuration,
remaining hosting/Meta dependencies and verification limits.

## Capability setup checklist

Open <http://127.0.0.1:4061/setup> for app-scoped configuration gaps and next steps across planning, messaging, voice, email, knowledge and connectors. The checklist is read-only; live verification is separate. See [setup and verification](docs/CAPABILITY_SETUP.md).

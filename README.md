# AI Agent Platform

Independent Python service extracted from NRAIAlgo on 2026-10-04. This project owns the runtime, model gateway, agent definitions, workflows, memory, PostgreSQL migrations, evaluation fixtures and service deployment. It has no Python, build or database dependency on the NRAIAlgo repository.

NRAIAlgo is the first consumer. Its application UI, login/session validation, HTTP facade and evidence connector stay in its own repository. The only shared boundary is the versioned HTTP/delegation contract in `docs/INTEGRATION_PROTOCOL.md`. The platform still includes NRAIAlgo-oriented research agents and tools; extraction does not make their contracts generic.

## Reuse across apps and businesses

The generic task API supports app-specific built-in catalogues, private user agents, configurable
planning limits and explicitly enabled business tool packages. New domains supply adapters and
versioned templates without editing the engine. See [multi-app onboarding](docs/MULTI_APP_PLATFORM.md)
and the separate [shipping tool example](examples/shipping_plugin/pyproject.toml).
Legacy NRAIAlgo research endpoints remain app-specific; use the generic task API for new apps.

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

This preserves the existing partial implementation. Scheduled triggers, notifications, broader trading RAG, production load tests and credentialed browser/provider verification remain unfinished. No live execution tools are present. This extraction does not declare production readiness.

The GitHub repository has not been created or connected. This folder is ready to use as its independent source project.

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

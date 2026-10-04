# AI Agent Platform

Independent Python service extracted from NRAIAlgo on 2026-10-04. This project owns the runtime, model gateway, agent definitions, workflows, memory, PostgreSQL migrations, evaluation fixtures and service deployment. It has no Python, build or database dependency on the NRAIAlgo repository.

NRAIAlgo is the first consumer. Its application UI, login/session validation, HTTP facade and evidence connector stay in its own repository. The only shared boundary is the versioned HTTP/delegation contract in `docs/INTEGRATION_PROTOCOL.md`. The platform still includes NRAIAlgo-oriented research agents and tools; extraction does not make their contracts generic.

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

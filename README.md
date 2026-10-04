# AI Agent Platform

Independent Python service extracted from NRAIAlgo on 2026-10-04. This project owns the runtime, model gateway, agent definitions, workflows, memory, PostgreSQL migrations, evaluation fixtures and service deployment. It has no Python, build or database dependency on the NRAIAlgo repository.

NRAIAlgo is the first consumer. Its application UI, login/session validation, HTTP facade and evidence connector stay in its own repository. The only shared boundary is the versioned HTTP/delegation contract in `docs/INTEGRATION_PROTOCOL.md`. The platform still includes NRAIAlgo-oriented research agents and tools; extraction does not make their contracts generic.

## Local setup

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# Configure private values in .env; the Python process requires exported environment variables.
python -m agent_platform.migrate
uvicorn agent_platform.app:app --host 127.0.0.1 --port 4060
```

Use a separate PostgreSQL database. The service never receives NRAIAlgo database or broker credentials. See `docs/AI_AGENT_DEPLOYMENT.md` for Docker setup and two-way networking.

```sh
python -m pytest -q
ruff check src tests
```

PostgreSQL tests are opt-in: migrate a disposable database, then set `AGENT_TEST_DATABASE_URL` to it. Unit tests use connector and model fixtures and require no NRAIAlgo checkout.

## Status

This preserves the existing partial implementation. Scheduled triggers, notifications, broader trading RAG, production load tests and credentialed browser/provider verification remain unfinished. No live execution tools are present. This extraction does not declare production readiness.

The GitHub repository has not been created or connected. This folder is ready to use as its independent source project.

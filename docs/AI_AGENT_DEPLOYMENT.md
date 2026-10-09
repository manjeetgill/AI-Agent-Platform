# Independent platform deployment

Build from this repository's Dockerfile. The image contains only the platform and its dependencies. `.env.example` lists platform settings; never commit a populated `.env`. Use a dedicated PostgreSQL database and a signing key of at least 32 characters shared only with the authorized consumer.

For a local deployment, copy `.env.example` to `.env`, replace the password in both `AGENT_DB_PASSWORD` and `AI_PLATFORM_DATABASE_URL`, and configure the connector URL, signing key, NVIDIA key and model names. Run `docker compose up --build -d`. The migration service runs after the database becomes healthy; the API starts only after migration succeeds. The API binds to localhost port 4060. `/readiness` verifies migration checksums.

For production, configure private routing between the two deployments. NRAIAlgo's `AI_PLATFORM_URL` must reach this API, and this service's `AI_PLATFORM_CONNECTOR_URL` must reach the NRAIAlgo API. Docker service names from one Compose project are not automatically reachable from another. Use your private service DNS or an explicitly provisioned shared network and TLS where traffic crosses hosts. Keep the API off the public internet.

NRAIAlgo keeps its own facade configuration (`AI_PLATFORM_ENABLED`, `AI_PLATFORM_URL`, `AI_PLATFORM_SIGNING_KEY`) and application deployment. Enable its flag after this service is ready. Roll back consumption by disabling that flag; preserve the platform database and reports.

Available platform flags include `AI_NVIDIA`, `AI_BUILTIN_AGENTS`, `AI_CUSTOM_AGENTS`, `AI_MEMORY`, `AI_RAG`, `AI_WORKFLOWS`, `AI_DURABLE_TASKS`, `AI_TASK_AGENTS` and `AI_AUTOMATIONS`. Orchestration is described in [ORCHESTRATION_API.md](ORCHESTRATION_API.md).

## Deployed pilot — 9 October 2026

Code release `428d53270177816120a970e5dac52189e75cd0a4` is deployed on the existing
DigitalOcean host at `https://agents.myalgodesk.in`. Migrations through 039 were
applied. API, task worker, WhatsApp worker and Meta worker use the new core image;
the voice worker uses a separate image retaining its native media dependencies.
Existing service credentials, channel bindings, database volumes and speech settings
were retained. MyAlgoDesk's application containers were not redeployed.

The HTTPS proxy exposes authenticated `/v1/task-agents`, `/v1/tasks`,
`/v1/orchestration/*` and integration-readiness routes. Credentials belong only in
an integrating backend. Operator console and OpenAPI remain unavailable publicly;
the Sahayak hostname does not expose orchestration routes.

Verified through the actual HTTPS API and server worker:

- Task `80d710d4-42e7-4eae-a158-87ce88a100a8` completed a typed comparison.
- A second owner was denied access to that task.
- An exactly approved one-shot schedule produced an in-app notification.
- Live-model task `10ce6ad5-6853-4479-b7b3-5d1604d1b666` completed
  `runtime.next → decision.compare → runtime.next`.
- The test agents and schedule were archived, and the temporary service key revoked.
- API readiness and MyAlgoDesk login returned 200; unauthenticated protected
  routes returned 401, and private routes returned 404.

`/opt/agent-platform/current-release.json` records the authoritative Compose file,
source revision and private backup directory. Use that Compose file for future
operations; older pilot overlays do not describe the current running images.
The release includes a private pre-migration database dump and rollback Compose
configuration. Do not restore the dump over new writes automatically. The guarded
deployment entry point is `deploy/release_orchestration.py`; stage a clean Git archive
with a `REVISION` file containing its full commit ID. It uses the existing release lock.

This deploys the platform, not DealAgent's Next.js app or retailer engine. Those
remain local. To move that consumer off the laptop, deploy its domain service and
authenticated frontend, install its tool package on the server, configure reachable
domain endpoints and provision an isolated server-side application credential.
Local credentials/database identities were not copied into the server database.
Off-host backup/restore, restricted database roles and public-customer load/security
verification remain rollout gates; this release is a deployed pilot.

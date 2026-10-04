# Independent platform deployment

Build from this repository's Dockerfile. The image contains only the platform and its dependencies. `.env.example` lists platform settings; never commit a populated `.env`. Use a dedicated PostgreSQL database and a signing key of at least 32 characters shared only with the authorized consumer.

For a local deployment, copy `.env.example` to `.env`, replace the password in both `AGENT_DB_PASSWORD` and `AI_PLATFORM_DATABASE_URL`, and configure the connector URL, signing key, NVIDIA key and model names. Run `docker compose up --build -d`. The migration service runs after the database becomes healthy; the API starts only after migration succeeds. The API binds to localhost port 4060. `/readiness` verifies migration checksums.

For production, configure private routing between the two deployments. NRAIAlgo's `AI_PLATFORM_URL` must reach this API, and this service's `AI_PLATFORM_CONNECTOR_URL` must reach the NRAIAlgo API. Docker service names from one Compose project are not automatically reachable from another. Use your private service DNS or an explicitly provisioned shared network and TLS where traffic crosses hosts. Keep the API off the public internet.

NRAIAlgo keeps its own facade configuration (`AI_PLATFORM_ENABLED`, `AI_PLATFORM_URL`, `AI_PLATFORM_SIGNING_KEY`) and application deployment. Enable its flag after this service is ready. Roll back consumption by disabling that flag; preserve the platform database and reports.

Available platform flags: `AI_NVIDIA`, `AI_BUILTIN_AGENTS`, `AI_CUSTOM_AGENTS`, `AI_MEMORY`, `AI_RAG`, `AI_WORKFLOWS`. Scheduler and execution requests remain unavailable. Production credentials and deployment have not been verified. Docker build and dependency security review remain release gates.

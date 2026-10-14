# Voice AI Platform --- Build, Verify, and End-to-End Completion Plan

**Purpose:** Build an independently owned developer platform inspired by
the capabilities of Vapi.ai. Extend the existing AI Agent Platform, implement
the remaining capabilities in controlled phases, and do not declare the project
complete until a repeatable end-to-end test passes.

**Status:** Local voice implementation and real synthetic WebRTC verification completed on 2026-10-11. Full master-plan acceptance is not complete; see the acceptance report for external blockers and unverified production requirements.
See [audit, architecture, test evidence and backlog](docs/VOICE_PHASE0_AUDIT.md).
Work directory selected by the user: `/Users/manjeet/Documents/ChatGPT/Algo/AI-Agent-Platform`.
The newer running checkout is comparison evidence only. [Local setup](docs/VOICE_LOCAL_SETUP.md), [acceptance report](reports/FINAL_ACCEPTANCE_REPORT.md) and [item-by-item status](reports/VOICE_REQUIREMENT_MATRIX.md) record the current scope.

**How to use this document:** Treat this as the master project
specification for Codex or another coding agent. Work from the first
unchecked item in order. Inspect the existing repository before making
assumptions. Implement, test, measure, fix, and retest. Update this
document with evidence as work is completed.

------------------------------------------------------------------------

## 1. Mission and completion definition

Build a multi-tenant voice-agent platform through which a customer can:

1.  Create an account and an organization/workspace.
2.  Create and configure a voice agent.
3.  Select interchangeable speech-recognition, language-model, and
    speech-synthesis providers.
4.  Test the agent in a browser without a telephone call.
5.  Connect tools and business APIs safely.
6.  Connect an authorized telephony/SIP provider and place or receive a
    test call.
7.  See live call status, transcripts, events, outcomes, duration,
    errors, and estimated cost.
8.  Manage API keys, webhooks, access permissions, usage limits, and
    billing data.
9.  Run automated tests and inspect reliable operational telemetry.

The platform should be modular: application/control plane, real-time
conversation/media path, model adapters, telephony adapters,
integrations, and observability must have clear boundaries.

### Definition of done

The project is **not done** because it compiles, the dashboard looks
finished, mocks return success, or a single happy-path demo works. It is
done only when:

-   All P0 requirements in this document are implemented or explicitly
    documented as blocked by an external dependency.
-   A clean environment can be provisioned from documented instructions.
-   Database migrations and seed/setup steps work from scratch.
-   Unit, integration, security, UI, and end-to-end tests pass with
    recorded results.
-   The real browser voice path works with real configured speech/model
    providers.
-   The real phone path works with an authorized test number/provider,
    if telephony is in scope for the release.
-   Failure scenarios are tested, including provider timeout,
    disconnect, invalid credentials, rate limits, and tool failures.
-   No unresolved critical/high security findings remain.
-   Latency, concurrency, reliability, and cost are measured against
    explicitly recorded targets.
-   No critical user journey depends on fake data or a stubbed success
    response.
-   Known limitations, external costs, provider requirements, and
    operational runbooks are documented.
-   The final report identifies each requirement as PASS, FAIL, BLOCKED,
    or NOT TESTED and links it to test evidence.

Never mark a requirement PASS without a test result, log, screenshot,
trace, or other reproducible evidence.

------------------------------------------------------------------------

## 2. Working rules for the coding agent

Use these rules for every phase.

1.  **Inspect first.** Examine the repository, branches, README, package
    manifests, architecture, tests, environment configuration, database
    schema, and existing implementation. Do not overwrite working code
    or assume a greenfield project.
2.  **Create a baseline.** Record current commit, runtime/tool versions,
    install/build/test status, known failures, and environment
    dependencies.
3.  **Plan small increments.** Break work into reviewable tasks. Keep
    changes focused and avoid unrelated rewrites.
4.  **Implement real behavior.** Do not substitute mock responses,
    hardcoded analytics, fake call states, placeholder buttons, or
    success-only stubs for production paths. Mocks are allowed only in
    clearly isolated tests.
5.  **Test after each change.** Run relevant unit and integration tests
    immediately; run broader regression tests after each phase.
6.  **Fix root causes.** Do not silence failures, weaken assertions,
    skip tests, or reduce security to obtain a green build.
7.  **Measure performance.** Capture baseline and post-change
    measurements. Do not claim "low latency," "fast," or "scalable"
    without data.
8.  **Protect secrets.** Never print, commit, or place real API keys,
    tokens, passwords, call recordings, or personal data in reports. Use
    secret-manager/environment references and redacted logs.
9.  **Ask only for true blockers.** Continue autonomously where safe. If
    credentials, paid provider access, a phone number, a signing key, or
    a user decision is essential, finish all independent work and report
    the exact blocker and how to resolve it. Never invent credentials or
    claim an external call was made when it was not.
10. **Keep this file current.** Check off an item only after its
    acceptance criteria pass. Add test commands, results, evidence
    locations, issues, and date/commit references.
11. **Never perform real outreach by accident.** Use designated test
    recipients/numbers and explicit opt-in. Do not launch campaigns or
    contact real customers during testing.
12. **Use safe changes.** Before destructive operations, migrations,
    data resets, dependency upgrades, or infrastructure changes, explain
    the impact and preserve recoverability.

For each task, report: **Goal → Repository findings → Changes made →
Tests run → Results → Remaining risks/blockers → Next task.**

------------------------------------------------------------------------

## 3. Scope and release strategy

### Release A --- Local developer MVP (P0)

-   Repository setup, local configuration, health checks, database
    migrations.
-   Authentication and tenant/workspace isolation.
-   Agent CRUD and validated configuration.
-   Provider adapter interfaces and at least one working configured
    speech/model path.
-   Browser-based live voice session.
-   Conversation state, transcript/events, and call/session history.
-   Tool execution with schemas, permissions, timeouts, and audit trail.
-   Structured logs, metrics, traces, and useful error states.
-   Automated tests and reproducible local end-to-end run.

### Release B --- Real telephone calling (P0 for phone release)

-   Telephony/SIP provider adapter, inbound/outbound call lifecycle.
-   Secure webhook validation and replay protection.
-   Call status reconciliation and disconnect/timeout handling.
-   Consent, recording policy, retention, and applicable telecom
    compliance.
-   Test number and authorized test destinations.
-   End-to-end inbound and outbound test calls with traceable evidence.

### Release C --- Production platform (P1)

-   API keys with scopes, rotation, and revocation.
-   Webhook delivery, retries, signatures, and event replay controls.
-   Usage metering, quotas, cost attribution, and billing integration.
-   Agent versioning, rollback, environment promotion.
-   Tenant admin roles, audit exports, data retention controls.
-   Load, soak, chaos/failure, backup/restore, and disaster-recovery
    testing.
-   Deployment pipeline, monitoring/alerting, runbooks, and incident
    procedures.

### Release D --- Scale and expansion (P2)

-   Additional speech, LLM, and telephony providers.
-   Provider routing/fallback policies and cost/quality routing.
-   Multi-agent handoffs and workflow builder.
-   WhatsApp and other channels, subject to platform approvals and
    policies.
-   Regional deployment and capacity autoscaling based on measured
    demand.
-   Self-hosted model inference where economics and quality justify it.

Do not start large-scale distributed infrastructure or GPU fleet work
before measuring real workload and validating demand.

------------------------------------------------------------------------

## 4. Suggested architecture

Keep the first version as a modular application with clear interfaces.
Extract services when performance, reliability, team ownership, or
deployment requirements justify the added complexity.

### A. Control plane

Responsible for user-facing and administrative operations:

-   Authentication, organizations, memberships, roles, tenant
    authorization.
-   Agent configuration, versioning, deployments, and secrets
    references.
-   API keys, usage plans, provider configuration, and audit logs.
-   Call/session search, analytics, usage, billing records, and admin
    controls.
-   REST API, SDK-facing contracts, validation, pagination, and rate
    limits.

### B. Real-time conversation/media path

Responsible for time-sensitive work:

-   Browser audio/WebRTC and/or telephony audio stream.
-   Voice activity and end-of-turn detection.
-   Streaming ASR, LLM generation, streaming TTS, interruption/barge-in.
-   Conversation state machine and cancellation propagation.
-   Bounded queues, backpressure, timeouts, and resource cleanup.
-   Audio format/sample-rate conversion only where required.
-   Call/session event stream and timing instrumentation.

Do not put slow reporting, analytics aggregation, or unrelated database
work in the critical audio path.

### C. Provider adapters

Define stable interfaces and normalize provider-specific behavior for:

-   Speech-to-text (ASR).
-   Text-to-speech (TTS).
-   Language models (LLM).
-   Telephony/SIP.
-   Optional embeddings, retrieval, and evaluation.

Adapters must expose capabilities, configuration validation, timeouts,
cancellation, streaming behavior, usage metadata, and normalized errors.
Provider switching must not require changes throughout the application.

### D. Tool/integration runtime

-   JSON-schema-validated input/output.
-   Allowlisted tools per agent/tenant.
-   Authentication references held server-side.
-   Least-privilege scopes and explicit authorization.
-   Timeouts, bounded retries, idempotency where applicable.
-   Per-tool rate limits and audit events.
-   SSRF protection, URL allowlists where appropriate, and
    private-network protections.
-   Human confirmation for high-impact actions where required.

### E. Data and infrastructure

A reasonable initial default, subject to repository constraints:

-   PostgreSQL for durable relational data.
-   Redis only where needed for ephemeral state, cache, locks, or rate
    limits.
-   Durable job/event system when asynchronous workloads require it.
-   S3-compatible storage for recordings and artifacts, with access and
    retention controls.
-   OpenTelemetry-compatible logs, metrics, and traces.
-   Containerized local development and repeatable CI.

Do not add Kafka, Kubernetes, a service mesh, or multiple databases by
default. Justify each operational dependency.

------------------------------------------------------------------------

## 5. Security, privacy, and reliability requirements

These are acceptance requirements, not optional polish.

### Identity and tenant isolation

-   Every tenant-owned record has a verified tenant/workspace
    relationship.
-   Every API operation checks authorization server-side.
-   Cross-tenant read/write tests must fail closed.
-   Role checks apply to API, UI, websocket, tool execution, recordings,
    and exports.
-   Session expiry, logout, revocation, and account recovery are tested.
-   Administrative access is least-privilege and auditable.

### Credentials and APIs

-   Never store provider secrets in plaintext application logs or
    client-side bundles.
-   Encrypt secrets at rest using an appropriate managed key/secrets
    mechanism.
-   Redact credentials, authorization headers, phone numbers, and
    sensitive transcript fields in logs.
-   API keys are shown only at creation where feasible, hashed for
    verification when suitable, scoped, rate-limited, rotatable, and
    revocable.
-   Webhooks use signature verification, timestamps/replay protection,
    and secret rotation.
-   Apply input validation, output validation, request size limits, rate
    limits, and safe error messages.

### Voice and tool safety

-   Treat audio, transcripts, retrieved documents, and tool output as
    untrusted input.
-   Do not allow spoken content to override system policy or grant new
    tool permissions.
-   Restrict tools to an explicit allowlist and tenant authorization.
-   Require idempotency or confirmation for sensitive actions such as
    payments, deletion, or external communications.
-   Propagate cancellation when a caller interrupts or disconnects.
-   Prevent duplicate tool calls after retries or reconnects.
-   Define behavior for silence, background noise, ambiguous requests,
    and unsupported languages.

### Privacy and compliance

-   Document what audio, transcripts, metadata, and provider payloads
    are stored and for how long.
-   Provide configurable retention/deletion and access controls.
-   Secure recordings and transcripts with tenant-scoped authorization.
-   Establish lawful basis/consent and recording announcements where
    applicable.
-   Review applicable Indian data protection, telecom, TRAI/DLT,
    carrier, and messaging-platform obligations with qualified
    counsel/provider documentation before launch.
-   Do not treat this checklist as legal advice or as a substitute for
    provider approval.

### Resilience

-   Explicit timeouts on every network call.
-   Retries only for eligible failures, with bounded exponential backoff
    and jitter.
-   Circuit breakers or equivalent protection where appropriate.
-   Bounded queues, backpressure, cancellation, and resource cleanup.
-   Idempotency for event processing and call lifecycle updates.
-   Graceful handling of provider outage, rate limiting, and partial
    failure.
-   Health/readiness checks, alerting, backups, restore drills, and
    incident runbooks.

------------------------------------------------------------------------

## 6. Performance and cost targets

Do not claim a universal latency number before selecting providers and
measuring the complete audio path. Establish targets after baseline
tests and report p50, p95, and p99.

Measure at least:

-   Time from end of user speech to first audible response.
-   Time to first ASR partial/final result.
-   Time to first LLM token.
-   Time to first TTS audio chunk.
-   Audio interruption/cancellation response time.
-   Dropped audio, jitter, reconnects, and failed sessions.
-   Concurrent active sessions, CPU/memory/network, queue depth.
-   Provider errors, timeout rate, tool latency, and call completion
    rate.
-   Cost per minute, per completed call, and per successful task.

For each metric, record: environment, hardware, region, provider/model
versions, test sample size, concurrency, warm/cold state, percentile,
and timestamp.

Create a baseline before optimization. Set explicit target thresholds in
a tracked decision record. If a target fails, profile the actual
bottleneck and retest after changes.

------------------------------------------------------------------------

## 7. Master execution checklist

### Phase 0 --- Repository and environment audit

-   [x] Inspect repository structure, current branch, current commit,
    and working tree.
-   [x] Identify frontend, backend, runtime versions, package managers,
    database, and infrastructure.
-   [x] Read project docs, configuration examples, migrations, CI,
    tests, and deployment scripts.
-   [x] Identify existing functionality and distinguish implemented code
    from placeholders/mocks.
-   [ ] Complete clean dependency installation and type checking. Fresh installation,
    wheel build and targeted provider-contract typing now pass; full-repository
    static typing remains incomplete.
-   [x] Record baseline failures and environment limitations.
-   [x] Produce an architecture map and dependency inventory.
-   [x] Produce a prioritized backlog with P0/P1/P2, dependencies, and
    acceptance tests.
-   [x] Update this document with actual repository findings before
    major changes.

**Exit gate:** We can explain how the application starts, how data
flows, what currently works, what is broken, and how to test it.

**Audit evidence:** [Phase 0 report](docs/VOICE_PHASE0_AUDIT.md). Original
checkout: 377 Python and 32 JavaScript tests passed, lint/build passed. Newer
comparison tree: 459 Python and 34 JavaScript tests passed, lint/build passed.
Installed dependency drift, stopped workspace, and database/source mismatch
are recorded; no real voice or telephone acceptance test was run.

### Phase 1 --- Reproducible developer environment

-   [x] Provide a complete README for supported operating systems.
-   [x] Provide `.env.example` containing placeholders only.
-   [ ] Validate required environment variables at startup with
    actionable errors.
-   [x] Document dependency installation, database startup, migrations,
    seed data, and shutdown.
-   [x] Add health and readiness endpoints.
-   [x] Add deterministic test fixtures and safe local test accounts.
-   [ ] Ensure no secret is committed and no production data is required
    for local development.
-   [ ] Verify a clean clone can be configured and started using the
    documented steps.

**Exit gate:** A new developer can start the app and run tests using
only the documentation and authorized credentials.

### Phase 2 --- Product contracts and data model

-   [ ] Define entities and relationships for users, organizations,
    memberships, agents, agent versions, providers, sessions/calls,
    messages/events, tools, API keys, webhooks, usage, and audit logs as
    applicable.
-   [ ] Define tenant ownership and authorization rules for every
    entity.
-   [x] Define API request/response schemas, error formats, pagination,
    and versioning.
-   [x] Define agent lifecycle and session/call state machines.
-   [x] Define event names, ordering expectations, idempotency, and
    correlation IDs.
-   [x] Add database constraints, indexes, migration rollback/recovery
    strategy, and data retention fields.
-   [x] Add contract tests and migration tests.

**Exit gate:** Contracts and lifecycle transitions are explicit,
versioned where necessary, and validated by automated tests.

### Phase 3 --- Authentication and multi-tenancy

-   [ ] Implement or verify sign-up/login and secure session management.
-   [ ] Implement organization/workspace creation and membership.
-   [x] Implement roles and permission checks.
-   [x] Enforce tenant scoping at the server/data-access layer.
-   [ ] Implement safe logout, expiry, revocation, and account recovery.
-   [ ] Add API-key creation, scope validation, rotation, revocation,
    and redaction if included in the release.
-   [x] Test unauthorized access, privilege escalation, IDOR, and
    cross-tenant reads/writes.
-   [ ] Verify websockets and file/recording endpoints enforce the same
    permissions.

**Exit gate:** Security tests prove one tenant cannot access another
tenant's data or actions.

### Phase 4 --- Agent configuration and provider adapters

-   [x] Implement agent create/read/update/delete and validation.
-   [ ] Support prompts, language, voice, model, greeting, turn-taking
    settings, and permitted tools.
-   [x] Version agent configuration and make deployed versions
    reproducible.
-   [ ] Define normalized ASR/LLM/TTS adapter contracts.
-   [x] Implement at least one real, configured end-to-end provider
    combination.
-   [ ] Add provider health, capability validation, timeout,
    cancellation, and error normalization.
-   [ ] Validate invalid model names, missing credentials, unsupported
    settings, and provider rate limits.
-   [x] Store only secret references in agent configuration; never
    expose provider secrets to the browser.
-   [x] Add unit and contract tests for adapters.

**Exit gate:** A saved agent configuration is validated, versioned,
securely stored, and can run through a real configured provider path.

### Phase 5 --- Browser voice experience

-   [ ] Implement microphone permission and device error handling.
-   [ ] Implement audio capture/streaming and output playback.
-   [ ] Implement session creation, start, stop, reconnect, and
    disconnect handling.
-   [ ] Implement streaming transcription and response playback.
-   [x] Implement interruption/barge-in and cancel obsolete model/TTS
    work.
-   [ ] Handle silence, noisy input, slow providers, partial transcript,
    and browser refresh.
-   [ ] Display accurate session states and timestamps.
-   [x] Persist messages/events and associate them with the correct
    tenant and agent version.
-   [ ] Add browser compatibility tests for the supported browser
    matrix.
-   [ ] Run a real microphone-to-real-provider-to-audible-response test.

**Exit gate:** A human can conduct a real browser voice conversation,
interrupt the agent, end the session, and retrieve its correct
transcript and event history.

### Phase 6 --- Tools and business integrations

-   [ ] Define a versioned tool schema and registration model.
-   [ ] Validate arguments and results against schemas.
-   [ ] Enforce per-agent and per-tenant allowlists and permissions.
-   [ ] Add authentication references, timeout, bounded retry,
    idempotency, and audit logging.
-   [ ] Prevent unauthorized internal-network requests and unsafe URL
    access.
-   [ ] Handle malformed results, tool errors, slow APIs, duplicate
    events, and caller disconnect.
-   [ ] Add tests proving a tool cannot execute without permission.
-   [ ] Build one real, harmless test integration (for example, lookup
    of seeded appointment data).

**Exit gate:** The agent can invoke an authorized tool, use the
validated result in its response, and safely handle failure without
duplicating side effects.

### Phase 7 --- Telephony and phone-call lifecycle

Required for the phone-calling release; mark BLOCKED if provider
credentials/numbering are unavailable.

-   [ ] Select an authorized telephony/SIP provider and document the
    decision and regional constraints.
-   [ ] Implement inbound and outbound call creation with provider
    adapter boundaries.
-   [ ] Verify webhook signatures and reject forged/replayed events.
-   [ ] Model ringing, answered, active, transferring, completed,
    failed, and cancelled states as applicable.
-   [ ] Stream bidirectional audio and normalize supported codecs/sample
    rates.
-   [ ] Implement hang-up, transfer, busy/no-answer, carrier failure,
    timeout, and disconnect behavior.
-   [ ] Reconcile out-of-order and duplicate provider events
    idempotently.
-   [ ] Use designated test numbers and explicit opt-in; never call
    arbitrary real users in tests.
-   [ ] Verify recording/consent and retention behavior.
-   [ ] Test inbound and outbound calls end to end using real authorized
    provider access.
-   [ ] Compare provider call duration and status with internal records.

**Exit gate:** Real authorized test calls work in both supported
directions, and every call has consistent lifecycle state,
transcript/events, duration, and failure reason.

### Phase 8 --- Observability and usage accounting

-   [ ] Add structured logs with request, tenant, agent, session/call,
    provider, and trace correlation IDs.
-   [x] Redact secrets and sensitive content by default.
-   [ ] Add metrics for errors, latency percentiles, concurrency,
    queues, provider status, and resource usage.
-   [ ] Add traces across media/session, ASR, LLM, TTS, tools, and
    telephony where technically possible.
-   [ ] Track provider usage and estimated costs with source and
    calculation version.
-   [ ] Separate estimates from final provider invoices/charges.
-   [x] Add filters and pagination to call/session history.
-   [ ] Alert on sustained error rates, latency regressions, resource
    saturation, and provider outages.
-   [ ] Verify telemetry itself does not expose credentials or sensitive
    transcript data.

**Exit gate:** An operator can trace a failed call, find its bottleneck,
and reconcile usage without exposing secrets.

### Phase 9 --- API, webhooks, deployment, and operations

-   [x] Publish API documentation and example requests.
-   [ ] Add authentication, scopes, rate limits, input limits, and
    versioning.
-   [ ] Implement signed webhook delivery, retry schedule, dead-letter
    handling, and safe replay.
-   [ ] Make deployment and rollback reproducible.
-   [ ] Add CI checks for lint, formatting, type checks,
    unit/integration tests, migrations, and security scans.
-   [ ] Configure production secrets, TLS, access controls, backups, and
    restore tests.
-   [ ] Add deployment health checks, graceful shutdown, and resource
    limits.
-   [ ] Document incident response, provider outage, credential
    rotation, data deletion, and recovery procedures.
-   [ ] Verify that development/test configuration cannot accidentally
    trigger production calls.

**Exit gate:** A controlled deployment can be released, monitored,
rolled back, and recovered using documented procedures.

### Phase 10 --- Performance, concurrency, and resilience

-   [ ] Establish repeatable benchmark scenarios and baseline
    measurements.
-   [ ] Load test session creation, agent configuration APIs, event
    delivery, and active voice sessions separately.
-   [ ] Test expected concurrency and a documented peak/burst profile.
-   [ ] Run a soak test long enough to reveal leaks and accumulating
    queues.
-   [ ] Simulate provider latency, timeout, rate limit, outage, and
    partial failure.
-   [ ] Simulate client network loss, reconnect, browser refresh, and
    caller hang-up.
-   [ ] Verify backpressure, cancellation, cleanup, and bounded
    memory/queue behavior.
-   [ ] Measure p50/p95/p99 latency, success rate, resource utilization,
    and cost.
-   [ ] Fix bottlenecks and rerun the same tests; compare before/after
    results.
-   [ ] Verify no duplicate calls or tool side effects occur during
    retries/reconnects.

**Exit gate:** The documented load profile meets recorded targets with
no critical correctness, security, or resource-leak failures.

### Phase 11 --- Security and release audit

-   [ ] Review authentication, authorization, tenant isolation, secrets,
    webhook verification, and API limits.
-   [ ] Test common web/API issues, including injection, XSS, CSRF where
    relevant, SSRF, IDOR, replay, and privilege escalation.
-   [ ] Check dependencies, container images, and deployment
    configuration for known vulnerabilities.
-   [ ] Verify transcript/recording access controls, retention,
    deletion, and redaction.
-   [ ] Verify tool execution and outbound communication cannot be
    abused to bypass tenant limits.
-   [ ] Verify backup and restore procedure and recovery assumptions.
-   [ ] Resolve all critical/high findings or document a formally
    accepted exception with mitigations and owner.
-   [ ] Check license obligations for dependencies, models, voices, and
    datasets.
-   [ ] Complete privacy, consent, provider, and applicable
    legal/compliance review before public launch.

**Exit gate:** Security review evidence exists, no unaccepted
critical/high issue remains, and release risks are approved by the
responsible owner.

### Phase 12 --- Final end-to-end acceptance

Run this phase from a clean environment using documented setup steps.

-   [ ] Create a new test user and organization.
-   [x] Verify tenant isolation with a second organization.
-   [x] Configure a real provider and create a voice agent.
-   [ ] Start a browser session and conduct a complete conversation.
-   [x] Interrupt the agent and verify cancellation/no stale speech.
-   [ ] Invoke an authorized test tool and verify its result.
-   [ ] Force a tool/provider failure and verify safe recovery.
-   [ ] End the session and verify transcript, events, timestamps, and
    usage.
-   [ ] Verify logs/traces let an operator follow the complete request
    path.
-   [ ] For phone release: place and receive authorized test calls, then
    test no-answer, hang-up, provider error, and webhook replay.
-   [ ] Verify usage and estimated cost are internally consistent.
-   [ ] Verify logout, revoked key, expired session, and unauthorized
    tenant access fail correctly.
-   [x] Run the full automated regression suite.
-   [ ] Run the documented load profile and confirm target metrics.
-   [ ] Verify no secret or sensitive test data appears in source
    control or logs.
-   [x] Generate the final test report with command, environment,
    result, evidence, and outstanding issues for every acceptance
    criterion.

**Exit gate:** Every applicable P0 acceptance test is PASS. BLOCKED
items have an explicit reason and are not represented as passed. The
release owner explicitly accepts any remaining limitations.

------------------------------------------------------------------------

## 8. Mandatory end-to-end test scenario

Use this as the main automated smoke test and as a manual acceptance
script.

### Scenario: customer creates and runs a real agent

1.  Start services from a clean documented environment.
2.  Create or sign in to a test account.
3.  Create Organization A and Organization B.
4.  Create an agent in Organization A with a configured, real provider.
5.  Attempt to read that agent as Organization B; assert `403` or a
    non-disclosing `404`.
6.  Start a browser voice session and assert a valid session ID and
    state transition.
7.  Speak a known test phrase; assert transcript data arrives and the
    agent produces audible output.
8.  Interrupt the output; assert generation/playback is cancelled and
    stale audio does not continue.
9.  Ask the agent to perform a harmless allowlisted lookup; assert
    validated tool input, one execution, and correct result.
10. Cause a controlled tool timeout; assert bounded handling and no
    duplicate side effect.
11. End the session; assert final state, timestamps, transcript, events,
    and usage record.
12. Query the trace/logs by correlation ID; assert that key stages can
    be inspected without secrets.
13. Revoke the test API key; assert subsequent use fails.
14. For telephony-enabled releases, execute inbound and outbound test
    calls through an authorized provider and verify lifecycle
    reconciliation.
15. Run cleanup and ensure test artifacts follow the configured
    retention policy.

Tests should assert observable outcomes, not internal implementation
details alone. Use deterministic test fixtures and provider test doubles
for failure tests, but keep at least one real-provider acceptance test
for each advertised real-time path.

------------------------------------------------------------------------

## 9. Test pyramid and required evidence

  -----------------------------------------------------------------------
  Test type               Purpose                 Required evidence
  ----------------------- ----------------------- -----------------------
  Unit                    State machines,         Test output and
                          validation,             coverage report
                          permissions, adapters   

  Contract                API, provider adapters, Passing contract suite
                          webhook schemas         

  Integration             Database, cache,        Test output and logs
                          queues, provider        
                          boundary                

  UI                      Main workflows,         Automated run and
                          loading/error/empty     screenshots where
                          states                  useful

  Security                Tenant isolation,       Test results and
                          credentials, replay,    finding log
                          tool access             

  End-to-end              Real user journey       Run ID, test report,
                          across the platform     logs/traces

  Performance             Latency, concurrency,   Benchmark artifacts and
                          soak, cost              environment details

  Recovery                Outage, rollback,       Drill record and
                          backup restore          recovery outcome
  -----------------------------------------------------------------------

Do not use code coverage as the sole proof of quality. Prioritize
meaningful assertions on money, access control, external side effects,
call state, and failure recovery.

------------------------------------------------------------------------

## 10. Final release report template

At the end, create `reports/FINAL_ACCEPTANCE_REPORT.md` containing:

-   Release/version and commit SHA.
-   Date, environment, hardware, provider/model versions, and
    configuration summary (no secrets).
-   Scope included and explicitly excluded.
-   Build, lint, typecheck, migration, and test commands with exit
    codes.
-   Results table: requirement, status, evidence link, defect/blocker,
    owner.
-   E2E run IDs, scenario results, and relevant trace/log references.
-   Performance table: baseline, target, measured p50/p95/p99,
    concurrency, pass/fail.
-   Reliability and failure-injection results.
-   Security findings and remediation status.
-   Estimated usage costs and how they were calculated.
-   Known limitations and external dependencies.
-   Deployment, rollback, backup, and recovery verification.
-   Final status: **PASS**, **PASS WITH EXPLICIT LIMITATIONS**, or
    **FAIL**.
-   A clear explanation of any item not tested or blocked.

Never report a full PASS if a required real-provider or end-to-end test
has not run.

------------------------------------------------------------------------

## 11. Initial instructions to Codex / the implementation agent

Paste this section as the first implementation task:

> You are the senior engineer responsible for delivering this Voice AI
> Platform. Read the complete `VOICE_AI_PLATFORM_MASTER_PLAN.md` before
> making changes.
>
> Start at Phase 0. Inspect the actual repository and report the
> architecture, current implementation, available scripts, baseline
> failures, and external dependencies. Do not assume the repository is
> empty, do not rewrite working code without justification, and do not
> jump directly to building UI features.
>
> Convert the phases into a prioritized implementation backlog with
> explicit acceptance criteria. Then implement one small, coherent task
> at a time. After every task, run the appropriate tests, fix
> regressions, and record the evidence. Update this master plan as
> criteria are completed. Never mark a box complete without evidence.
>
> Build real end-to-end functionality. Clearly identify mocks and keep
> them isolated to tests. Do not invent API credentials, bypass provider
> requirements, trigger unauthorized calls, expose secrets, or claim
> tests passed when they did not run.
>
> When blocked by an external dependency, continue all independent work
> and report the precise missing input, why it is needed, and the exact
> setup step required. Do not repeatedly ask questions that the
> repository or documentation can answer.
>
> Do not optimize for code volume or a superficially polished dashboard.
> Optimize for correctness, secure tenant isolation, predictable
> call/session state, real-time responsiveness, observability,
> recoverability, and maintainability.
>
> At the end of each work session, report: (1) completed work, (2) files
> changed, (3) commands run and actual results, (4) measured performance
> where relevant, (5) open bugs/risks/blockers, (6) the next smallest
> task, and (7) the updated checklist. Continue until the applicable
> acceptance criteria are satisfied.
>
> The project is complete only after Phase 12 passes with reproducible
> evidence. If any test cannot run, mark it BLOCKED or NOT TESTED; never
> claim completion based on assumptions.

------------------------------------------------------------------------

## 12. Decision log

Record decisions here rather than silently hardcoding assumptions.

  -----------------------------------------------------------------------
  Decision                Current default         Revisit when
  ----------------------- ----------------------- -----------------------
  Initial release         Browser voice MVP, then Real telephony
                          phone calling           credentials and test
                                                  number are ready

  Backend architecture    Modular application     Measured scaling/team
                          plus isolated real-time needs justify service
                          media path              extraction

  Model strategy          Provider-neutral        Quality, latency,
                          interfaces; at least    licensing, and cost
                          one real provider path  data are available

  Storage                 PostgreSQL; object      Scale or retention
                          storage only for        requirements change
                          retained                
                          media/artifacts         

  Queue/event system      Start simple; add       Measured asynchronous
                          durable broker when     load and delivery
                          justified               guarantees require it

  Deployment              Reproducible local      Production SLO,
                          environment and CI      traffic, and regional
                          first                   needs are defined

  Performance targets     Establish baseline,     First end-to-end
                          then approve explicit   benchmarks are
                          thresholds              available

  Compliance              Review before           Target countries,
                          production              channels, and providers
                          telephony/outreach      are finalized
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 13. Current project status

**LOCAL AUDIO PILOT VERIFIED; FULL MASTER-PLAN ACCEPTANCE NOT COMPLETE.**

- [x] Original checkout isolated from newer running services/databases.
- [x] Fresh virtual environment, migrations, API/workspace and durable task worker.
- [x] Per-agent provider configuration, versions, restore and session snapshots.
- [x] Real local ASR → model → TTS over WebRTC, including interruption and final call state.
- [x] App/caller isolation, transcript consent/expiry/deletion, bounded usage.
- [x] Synthetic database backup/restore and security dependency remediation.
- [x] Regression evidence and detailed acceptance report.
- [x] Source-validated streamed quotations, speech-aware VAD and mute control.
- [x] Restricted local runtime role; per-stage usage estimates and reserved spending limits.
- [ ] Human microphone acceptance: user Chrome tab opens and connects; complete spoken reply after VAD fix still needs confirmation.
- [ ] Authorized telephone calls: carrier/number absent; outbound bridge remains disabled.
- [ ] Public production gates: capacity/soak, network, operational/security and provider licensing reviews.

See [the final report](reports/FINAL_ACCEPTANCE_REPORT.md) and
[requirement matrix](reports/VOICE_REQUIREMENT_MATRIX.md). Unchecked items are
not silently treated as finished. A passing synthetic call is not a production
release or an authorized telephone call. All changes remain in this original
checkout; the newer checkout/database was not changed.

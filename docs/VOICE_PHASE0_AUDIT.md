# Voice platform: Phase 0 baseline

Date: 2026-10-11. Scope: repository and environment audit for the supplied Voice AI master plan. **Audit completed; browser voice and telephone release acceptance are NOT VERIFIED.** No paid provider call or customer outreach was performed.

## 1. Repository decision

Two checkouts were found. Both were clean before this audit.

| Checkout | Main commit | Migrations | Use in this audit |
| --- | --- | --- | --- |
| `/Users/manjeet/AI-Agent-Python-clean` | `d8072a26fd5900489bb44e347c4f9f1530e6e17c` | 57 | Running-service comparison only; no source changes here |
| `/Users/manjeet/Documents/ChatGPT/Algo/AI-Agent-Platform` | `9d59fa713cfaaf81c865597f5e886c99e95e8914` | 49 | User-selected implementation folder and primary baseline |

The older commit is not an ancestor of the newer checkout's HEAD. Do not merge these trees or downgrade a database based on migration counts alone. The user explicitly chose the original task directory for voice work. Keep all implementation there. The newer checkout was audited only to explain the live-service/database mismatch. Use isolated database/ports for original-tree development until an explicit compatibility/synchronization decision; do not run its launcher against the newer service database. This report changes no remotes, branches, source behavior or deployment.

Both existing local databases contain 57 migrations. The older tree's 49 migration checksums match their corresponding applied entries, but migrations 050–057 are absent from that source tree. Those include row security, identity bootstrap, delegation replay protection, provider chains/quotas, and SMS adapters. The newer tree already has these and a CI workflow. Missing CI and role isolation in the older tree must not be reported as missing from the running platform.

## 2. Architecture and data flow

```text
Consuming app (customer login, business data, booking/business tools)
  -> scoped platform API / signed delegation
  -> FastAPI control plane (agents, tasks, configuration, sessions)
       -> PostgreSQL (tenant state, migrations, durable queue, audit, knowledge)
       -> task worker (claims, approvals, retries, callbacks)
       -> model gateway and enabled business-tool adapters

Browser voice client
  -> authorized voice session + WebRTC/aiortc media
  -> utterance buffer -> speech recognition -> voice reasoning / app handler
  -> speech synthesis -> audio playback
       -> durable session/turn metadata in PostgreSQL
       -> in-process media buffers and short conversation history

Authorized app-owned carrier -> Asterisk/AudioSocket -> voice handler
WhatsApp/Chakra/Meta adapters -> scoped business routing -> app handlers
Operator workspace (4061) / API monitor (4060) -> platform APIs
```

The original tree lacks the newer tree’s restricted-role scripts and migrations 050–057. References below to restricted-role provisioning and CI describe the newer comparison tree, not functionality already present in the selected source.

The platform is an existing Python service with a vanilla HTML/CSS/JavaScript operator workspace, not an empty repository. Extend its tenant identity, durable worker, tools, memory and adapters. Customer authentication normally belongs to consuming apps; a public self-service account portal is additional product scope.

Startup is owned by `scripts/local.py`: private configuration, local PostgreSQL, migrations and API/worker lifecycle. Restricted database provisioning is in `scripts/database_roles.py`; deployment guidance is in `docs/AI_AGENT_DEPLOYMENT.md`. The original tree has no CI workflow. The newer tree has CI at `.github/workflows/tests.yml` which runs Python 3.12, PostgreSQL 17, Node 22, restricted-role tests and both base/voice dependency variants. CI existence is verified; a hosted CI run was not executed here.

## 3. Dependency and environment inventory

| Layer | Observed version / state |
| --- | --- |
| Host | macOS 26.6.2, arm64; approximately 49.6 GB free at audit |
| Python | 3.14.7; original checkout has `.venv`; newer checkout has no dedicated `.venv` |
| Audit interpreter | Original checkout's `.venv`; current tests import current `src` through pytest configuration |
| API / validation | FastAPI 0.141.1, Pydantic 2.13.5, Uvicorn 0.53.0 |
| HTTP / database | HTTPX 0.28.1, psycopg 3.3.6, psycopg-pool 3.3.3; PostgreSQL 17.11 Homebrew |
| Voice | aiortc 1.15.0, PyAV 17.1.0 |
| Build / tests | setuptools 84.0.0, pip 26.2.1, pytest 9.1.1, Ruff 0.16.10; Node 22.13.0 |
| Dependency drift | Installed cryptography 50.0.2; source pins 46.0.7 |
| Local speech | `whisper-cli` and `espeak-ng` absent from PATH; alternate binary paths/model assets not verified |
| Type checking | No configured type-checker command in the inspected project manifest |

`pip check` passes for the installed environment. That does not resolve the mismatch with the source pin. No dependency upgrade/downgrade was attempted in this audit. A fresh install, Linux container build and model/voice license review remain Phase 1 work.

## 4. What exists and what remains

| Capability | Source evidence in `src/agent_platform/` | Finding |
| --- | --- | --- |
| Multi-app identity, queue, approvals, RAG and memory | API/runtime modules, migrations, restricted-role tests | Reuse existing foundation; passing suite is not a complete security audit |
| Voice-agent CRUD and browser session API | `web_voice.py`, `console/` | Implemented pilot; live browser/microphone acceptance not run |
| Speech selection | `calling_audio.py` | Gemini, Sarvam and local adapter paths; selection is environment-wide |
| Voice reasoning | `web_voice.py:59` | Hardwired Gemini gateway; generic provider chains do not make this path replaceable |
| Conversation media | `calling_media.py` | Utterance-based buffering, then recognition, reasoning and synthesized playback; not full streaming |
| Interruption | `calling_media.py:130` | Incoming speech discarded while busy or playback buffered; barge-in absent |
| Session state | `web_voice.py`, voice migrations | Durable session status/turn metadata; media and recent conversation history remain process-local |
| Voice configuration versions | `web_voice.py` | No immutable deployed voice configuration snapshot bound to each session |
| Voice history / telemetry | Session/turn records | Input hashes and outcomes do not provide a complete searchable transcript, stage trace or cost ledger |
| Business tools | Signed app handlers; generic task tool framework | Existing integration boundary; unify voice invocation with permissions, cancellation and audit contracts |
| Carrier bridge | `telephony.py`, Asterisk integration | App-owned carrier required; outbound support is explicitly disabled in this bridge |
| WhatsApp/browser handoff | Existing calling and handoff modules | A browser talk link is not proof of WhatsApp calling eligibility or telephone delivery |
| Service choices | `service_options.py` | Setup requests expose pending state; a selection does not automatically activate local speech or a provider |

Current browser pilot limits include a 120-second session, 15 turns, 30 starts per app/day and installation-wide concurrent capacity of two. These are limits found in code, not proven performance capacity. Live media needs process ownership/sticky routing before multiple API replicas can handle sessions safely. Recording and live telephone transfer are not enabled by this audit.

## 5. Tests and results

Evidence: `docs/verification/voice-phase0-2026-10-11/` contains command metadata and inspected output for both checkouts. Tests ran against disposable databases, not customer tables. Provider calls were mocked in tests; there was no microphone or real phone test.

| Check | Result |
| --- | --- |
| Current `scripts/test_restricted.py tests/python -p no:cacheprovider` | **459 passed**, one warning, pytest 175.66 s; wrapper 178.66 s |
| Current Node suite: extension, node and JavaScript tests | **34 passed**, no failures/skips |
| Current Ruff: `check --no-cache src scripts tests/python --output-format json` | PASS |
| Current local wheel build, no network/dependency install | Initial sandbox file-write failure; retry with build-directory access PASS |
| Original checkout Python suite | 377 passed after database startup |
| Original checkout JavaScript suite | 32 passed |
| Original checkout lint, local wheel build, installed dependency check | PASS |
| Clean install, independent virtual environment, type check | NOT TESTED / no type-check command configured |
| Real ASR/LLM/TTS, microphone playback, barge-in, calls, load/soak | NOT TESTED |

Initial Python execution failed because PostgreSQL on 55460 was stopped. The existing local database was started. An existing supervisor then brought up the API from the newer checkout. API `/health` and `/readiness` returned 200; the operator workspace on 4061 was unavailable. No downgrade or live migration was performed. The database inspection used the bootstrap operator credentials; their privileged role flags are **not evidence** that the running API uses a superuser. Actual live API role verification remains a deployment check.

The pytest warning concerns Starlette's HTTPX TestClient deprecation. It did not fail tests. The initial current-build failure was an OS sandbox restriction on generated build files, not a packaging defect; the successful retry is recorded separately rather than concealing the first failure.

## 6. Prioritized implementation backlog

Order dependencies before dependent work. Existing code is verified/extended, not reimplemented wholesale.

| ID | Priority / phase | Work | Depends on | Acceptance |
| --- | --- | --- | --- | --- |
| V01 | P0 / 1 | Keep the user-selected original checkout; isolate database and ports from newer service; reconcile dependency pin; correct startup docs | Phase 0 | Fresh setup with pinned dependencies, disposable migrations, API + worker + workspace readiness; no customer/provider dispatch |
| V02 | P0 / 2–3 | Document voice data contracts and verify tenant/delegation/session ownership | V01 | Port or independently implement missing restricted-role/RLS protections in the selected tree; two-app negative authorization tests; runtime uses intended restricted roles |
| V03 | P0 / 4 | Immutable voice-agent versions and normalized ASR/LLM/TTS contracts; per-agent service choice | V02 | Sessions pin versions; invalid/unsupported configs rejected; explicit configured provider selection without hidden fallback |
| V04 | P0 / 5 | Streaming media/recognition/playback, cancellation and barge-in | V03 | Interrupt speaking agent; obsolete output stops; no stale speech/tool action; bounded queues and timeout tests |
| V05 | P0 / 5,8 | Tenant-scoped transcripts/events, history, stage timings and usage accounting | V02–V04 | Complete session timeline, redaction/retention, ordered events, measured latency and explicit unknown cost |
| V06 | P0 / 6 | Common voice business-tool contract | V03 | Two app handlers, schemas/grants, bounded timeouts, reconciliation on uncertain side effects, financial approval preserved |
| V07 | P0 / A acceptance | Provider-backed and platform-operated browser pilot | V04–V06 | Real microphone conversation and interruption, tool result, failure path, history; self-hosted assets verified before activation |
| V08 | P0 for B / 7 | Authorized carrier + Asterisk inbound/outbound lifecycle | V06–V07, app-owned carrier access | Authorized real inbound/outbound tests; statuses, no-answer, hang-up and duplicate-event reconciliation |
| V09 | P1 / 9–11 | Deployment, media ownership, TLS, backup/restore, quotas, privacy/security review | V05–V07 | Independent setup, restore drill, secret checks and realistic load/soak metrics |
| V10 | P2 / D | Horizontal media scaling and advanced routing/analytics | Measured V09 capacity | Repeatable target concurrency and failure recovery; cost/capacity evidence |

Local speech can remove an external speech API charge, but still needs installed models, CPU/GPU capacity and operating resources. Telephone connectivity remains the consuming app's authorized carrier dependency. Do not promise zero-cost mobile calls or convert a saved provider key into an acceptance PASS.

## 7. Changes and next task

Added the adapted root master plan, this audit, prioritized backlog and test evidence. No voice code, customer settings, credentials, remotes or live integrations changed. All saved documentation is in the user-selected original folder; the newer checkout remains unchanged. No commit, push or deployment performed.

**Next task: V01 / Phase 1.** Establish the original checkout's isolated environment and reproducible startup before implementing the versioned provider-neutral voice configuration. No additional provider credentials are needed for that work. The master plan's later checkboxes remain unchecked until their acceptance tests are run.

# Voice platform acceptance report — 2026-10-11

## Result

**Local synthetic voice pilot: PASS WITH EXPLICIT LIMITATIONS.**
**Entire master plan / public and telephone release: FAIL (acceptance incomplete).**

The local audio path is implemented and repeatably verified with real local
recognition, reasoning and synthesis. A synthetic caller also interrupted the
greeting successfully. This is not a human microphone test, public-network
test, telephone call, production certification or completion of every phase.
The [requirement matrix](VOICE_REQUIREMENT_MATRIX.md) preserves unfinished items.

## Environment and scope

- Original checkout, local `main`; original base `9d59fa713cfaaf81c865597f5e886c99e95e8914`, now reconciled with production `13924408f65cb579238d47a8bc7292534b98f754` with user approval.
  Changes are uncommitted working-tree changes. No GitHub push or deployment.
- macOS arm64, Apple M4 Pro, 48 GiB RAM; Python 3.14.7; PostgreSQL 17.11; aiortc 1.15.0;
  whisper.cpp 1.9.5; eSpeak NG 1.52.0; Ollama 0.34.3.
- Isolated environment `.venv-voice`; database 55560; API 4160; workspace 4161;
  private media 4162; local model listener 11435. Existing 4060/4061 services/newer checkout unchanged.
- ASR: multilingual `ggml-small.bin`, English mode for this test. Model:
  cached `qwen2.5:3b`. TTS: local eSpeak NG. No cloud provider credentials used.
- Canonical new migrations 058–064 follow production security/SMS 050–057: voice configuration, lifecycle, cancellation, costs, media claims and isolation.
  The old pilot with voice migrations 050–054 is preserved; a new database tests the reconciled lineage.
  Disposable regression databases start from scratch.

## Delivered changes

1. Reproducible isolated launcher, private setup helper, source/database manifest
   guard and CI definition for base/voice installs.
2. Validated per-agent provider/model and turn-taking configuration. Exact model
   pinning is separate from general model routing. Unconfigured providers fail closed.
3. Agent CRUD, optimistic version conflicts, session snapshots, history and
   restoration as a new version. Restoring rechecks knowledge permissions and
   does not re-enable a paused agent.
4. Interruptible WebRTC playback, cancellation fences, bounded sentence TTS,
   optional local provisional transcription, disconnect and turn deadlines.
5. Ordered session events, stage timings, consented 24-hour transcripts, early
   transcript deletion and worker-driven expiry. Live browser voice content does
   not become durable knowledge-answer or memory-learning text.
6. App/caller-scoped call history and count/time limits. Per-stage usage estimates,
   operator rate cards and app-level atomic spend reservations now exist. Unknown
   attempts retain their estimated hold; this is not invoice reconciliation.
7. Workspace provider/version/history controls and widget transcript/caption controls.
8. Real synthetic WebRTC verification, restore drill and dependency audit.
9. Speech-aware VAD, a microphone mute button, and opt-in SSE delivery of complete
   source quotations validated before synthesis. General fluent answers remain buffered.
10. Approved restricted pilot runtime role, deployment preflight and missing public
    voice-route configuration. Public configuration has not been deployed.

## Test evidence

Commands run from this repository using `.venv-voice`. Paths below contain no
private credentials; synthetic transcript content is explicitly labelled.

| Check | Actual result | Evidence |
| --- | --- | --- |
| Fresh `pip install -e '.[dev,voice,audit]'` | PASS; separate environment | `pyproject.toml`, setup instructions |
| `pip check` | PASS, no broken requirements | [command results](../docs/verification/voice-implementation-2026-10-11/checks.txt) |
| Disposable full Python suite after dependency and FAQ-cancellation fixes | **386 passed**, one Starlette/httpx deprecation warning; 115.30 s | [Python output](../docs/verification/voice-implementation-2026-10-11/python-tests.txt) |
| Final media-failure state regression | **28 passed**, one deprecation warning | [focused output](../docs/verification/voice-implementation-2026-10-11/failure-state-tests.txt) |
| Node extension/workspace suites | **34 passed**, zero failures/skips | [JavaScript output](../docs/verification/voice-implementation-2026-10-11/javascript-tests.txt) |
| Ruff on source/tests and changed lifecycle/verification scripts | PASS | [command results](../docs/verification/voice-implementation-2026-10-11/checks.txt) |
| JavaScript syntax checks, diff whitespace check | PASS | [command results](../docs/verification/voice-implementation-2026-10-11/checks.txt) |
| `mypy` | PASS for the new provider-configuration boundary only | [type check](../docs/verification/voice-implementation-2026-10-11/typecheck.txt) |
| Dependency vulnerability audit | Initially flagged old cryptography; final scan reports no known vulnerabilities | [before](../docs/verification/voice-implementation-2026-10-11/dependency-audit-before.json), [after](../docs/verification/voice-implementation-2026-10-11/dependency-audit-after.json) |
| Known local credential/private-key-marker scan of changed files | PASS within stated scan scope | [scan](../docs/verification/voice-implementation-2026-10-11/secret-check.json) |
| API and workspace readiness | Both HTTP 200, correct current HTML served | [readiness](../docs/verification/voice-readiness.json) |
| Actual local WebRTC → ASR → model → TTS → received audio | PASS, asserted both opening/closing hours and ENDED state | [all measured runs](../docs/verification/voice-local-e2e-runs.json) |
| Actual interruption during greeting | PASS, interruption event and correct subsequent spoken answer | [latest audio run](../docs/verification/voice-local-e2e.json) |
| Database backup/restore | PASS, 10 agents/versions/sessions and 91 events restored, zero ownership mismatches | [recovery](../docs/verification/voice-recovery.json) |
| Wheel build | PASS, isolated setuptools build | [build output](../docs/verification/voice-implementation-2026-10-11/wheel-build.txt) |
| External Chrome UI / human microphone | Page loads and microphone connected; complete spoken reply NOT CONFIRMED. Repeated interruptions observed before VAD fix. | [content-free human evidence](../docs/verification/voice-human-microphone.json) |
| Real inbound/outbound telephone call | BLOCKED / not attempted | No authorized carrier connection in this isolated profile |
| Hosted CI, Linux/Windows install, mobile/public network | NOT TESTED | CI configuration is not a hosted test result |

The original baseline had 377 Python tests. The new tests specifically cover
configuration isolation, exact model pinning, version conflicts/restoration,
foreign-app/user denial, transcript opt-in/deletion, quotas, stale synthesis,
partial-transcript cancellation, speech failure and interruption semantics.
The existing suite covers other apps and channel integrations using test doubles.
Those passing tests do not mean every cloud speech provider was exercised live.

### Failures fixed during verification

- Base Whisper/eSpeak output confused spoken numbers. Switched the pilot to
  multilingual small, clarified clock pronunciation and slowed local speech.
  The final received audio must still contain both nine and five; assertions
  were not replaced with a mere “audio exists” check.
- Cancelling an FAQ answer left an UNKNOWN turn blocking the next question.
  Read-only cancellation now records CANCELLED. An interrupted external action
  still records UNKNOWN and cannot be blindly retried.
- The source pinned cryptography 46.0.7. The audit reported seven advisory
  entries (including duplicates). Updated to 50.0.2; compatible pyOpenSSL resolved
  to 26.4.0, and the full suite plus WebRTC test passed afterwards.
  Security fixes are documented in the [official cryptography changelog](https://cryptography.io/en/latest/changelog/).
- A first wheel command used `--no-build-isolation` without setuptools in the
  fresh environment. The documented isolated build installs its declared build
  dependency; this was an environment setup failure, not a successful build.

## Performance and cost

These are baseline smoke observations, not a production capacity claim.
Same local Mac, cached model, English generated input, one active audio caller.
Other local development/tests may contend for resources.

| Observation | Value |
| --- | --- |
| Last generated input frame → first received reply audio, ordinary runs | 2,641 ms; 3,523 ms |
| Same metric while interrupting greeting | 5,140 ms; 4,681 ms |
| Complete verification including greeting, reply playback and checking | Approximately 16–21 seconds/run |
| API-provider fees for these local runs | ₹0; no external ASR/LLM/TTS API requests |
| Concurrency actually live-tested | Two synthetic WebRTC callers across separate apps; six calls PASS, extra caller rejected, foreign-app session reads denied |
| Production p50/p95/p99, soak, mobile latency | NOT TESTED; four smoke runs are insufficient |

Adopt an initial *future test* budget of eight seconds for first reply audio at
one local caller. This is not an approved production SLO. Establish capacity
targets, run sufficient warm/cold samples and measure p50/p95/p99 before raising
the two-call limit. Model licenses, electricity, hardware, hosting, TURN and
telephone charges are separate from zero local API-provider fees. Runtime
API cost estimates now use operator-verified rate snapshots. Local external API
fees are zero. No customer invoice is generated.

## Security, reliability and operational boundaries

The operator console is loopback-only. App backends hold scoped service keys
and attest their authenticated external users. Voice queries scope both app
and owner. Arbitrary public model URLs cannot be supplied in agent JSON.
No customer login, key or phone call was performed in this verification.

The production row-level-security and SMS baseline has been reconciled into the
selected original folder. The new isolated pilot uses separate restricted API,
worker and migration roles. Media uses the scoped API role and retains each call's
identity for asynchronous callbacks. New tables have reviewed isolation policies;
runtime cannot change rates or spending authority. This is not a penetration test.

The [dedicated media service](../docs/VOICE_MEDIA_SERVICE.md) now owns audio.
A synthetic conversation survived restarting its temporary control API and
completed with the correct audible answer. A media-process crash still ends calls;
no automatic resume/replay is claimed. Private control requests are HMAC-signed,
replay-protected and bound to an existing session reservation. The local development
supervisor still shuts down companions together; production needs independent services.
Transcript deletion disables subsequent retention, expiry is enforced on reads
and by the worker, and no call audio archive is created. Caller content can
still reach an explicitly configured app handler, whose logs need their own policy.

Recovery was a real local database dump/restore to a disposable DB. It did not
exercise offsite backups, encryption-key loss, hosted disaster recovery or
restoring a live media stream. Migration rollback means restoring a matching
code/database backup, never downgrading the separate newer running database.

## What prevents full master-plan completion

| Requirement | Status / exact remaining work |
| --- | --- |
| Human browser acceptance | External Chrome page opens and a microphone call connected. Complete reply after speech-VAD fix awaits human confirmation. No browser protections were bypassed. |
| Public customer release | PENDING: SSH now works. The conflicting source/migration lineage was reconciled locally; the combined release is not yet deployed. Route and private-media deployment changes are prepared. TURN, real relay/mobile tests, production soak, offsite/key recovery and operational/security review remain. |
| Real phone release | BLOCKED by authorized app-owned carrier/SIP access and designated test destinations. The existing outbound bridge remains disabled; outbound/transfer implementation and real-carrier tests are not finished. |
| Streaming | Complete source quotations now stream through SSE and pass real audio tests. ASR uses bounded rolling decodes, not a native continuous ASR stream. Arbitrary model-token speech and fluent streaming translation remain outside this implementation. |
| Full provider billing | Rate snapshots, unit ledger, estimated cost, uncertain holds and atomic app limits implemented. Provider invoice reconciliation, carrier costs and customer charging remain incomplete. No real paid rates have been configured or provider billing tested. |
| Full-repository static typing, public account portal | NOT COMPLETE. New config boundary is typed; existing platform consumes authenticated app identities rather than providing a new public signup product. |
| Full master acceptance | FAIL until the above applicable gates pass; no blanket production-ready claim. |

The master checklist and matrix retain these gaps. Further engineering is needed for native streaming ASR, invoice reconciliation,
full typing and production operations. Human, carrier and deployment gates also
require the relevant access or owner action.

## Use and reproduce

Open [the isolated voice workspace](http://127.0.0.1:4161/voice) and select
**Local voice demo — synthetic clinic** for the prepared FAQ-only agent.
See [setup, APIs, retention and runbook](../docs/VOICE_LOCAL_SETUP.md) for exact
commands and how another app integrates through its authenticated backend.
All implementation work remains in the user's selected original folder.


## Follow-up verification: streaming, runtime and capacity

The following results supersede the earlier single-call baseline where applicable:

- Complete streamed audio test: PASS, session `6ca8a9ff-3457-47ba-9426-b5bf4993bfd9`,
  2,302 ms from final input frame to received reply audio.
- Interruption plus streaming: PASS, session `007112ed-7413-4c28-aa8e-e249a1852c80`,
  2,041 ms by the same metric. The recorded interruption occurred during greeting.
- [Six-call/two-app test](../docs/verification/voice-load.json): PASS, three waves,
  two simultaneous callers, p50 2,247 ms and p95/p99 3,302 ms. All calls ENDED,
  all extra-call attempts rejected, all foreign-app session reads denied. This is
  a small local smoke sample, not a production load/soak certification.
- First concurrent attempts failed received-audio validation because recognition
  heard “night” instead of “nine” in the synthesized full sentence. Slowing local
  synthesis from 150 to 130 words/minute fixed the repeated two-call fixture.
  Assertions still require both opening and closing hours. Prior failures are
  retained under [follow-up evidence](../docs/verification/voice-production-2026-10-11/).
- Full Python regression during this follow-up: 390 passed before the final VAD
  change. Final targeted database/streaming/VAD tests: 14 passed, zero skips.
  Final JavaScript suites: 34 passed. Ruff: PASS. Dependency audit after adding
  WebRTC VAD: no known vulnerabilities reported. One Starlette/httpx deprecation
  warning remains in Python tests.
- [Restricted runtime proof](../docs/verification/voice-runtime-role.json): PASS.
  Private runtime configuration stays outside Git. The role and process restart
  were explicitly approved by the owner.
- [Deployment preflight](../docs/verification/voice-deployment-preflight.json):
  exact local migration manifest, runtime role, no active calls and audio/API
  readiness pass. Public HTTPS page responds, but public voice APIs return 404;
  TURN and separate production acceptance gates remain blocked. No remote files
  or databases were modified.

The local pilot is running at port 4161 under the restricted role. The human test
is still pending after the microphone/echo interruption fixes. No telephone calls
were placed, no paid API request was made, and no production rollout is claimed.

## Media extraction and reconciliation update

- Before reconciliation: full Python suite **395 passed**; ten local synthetic
  calls passed; a separate API restart preserved a complete voice conversation.
- After reconciliation: initial full run **468 passed, 2 failed**. The failures
  identified a missing merged role helper and the explicit global replay-table
  classification. Both were fixed; the focused rerun **14 passed**. The final combined run **472 passed**, one deprecation warning, in 161.71 seconds.
  [Full output](../docs/verification/voice-media-2026-10-11/python-tests.txt).
- Six real synthetic calls across two app identities: **PASS**, two concurrent
  callers, extra admission rejected, foreign-app reads denied. p50 2,223 ms;
  p95/p99 2,943 ms. This is a small local sample, not a production soak.
- API restart with production-style database isolation: **PASS**, session
  `97a393ae-efc8-46c6-ab9d-00214d6f1353`; correct audio answer and final ENDED.
- Sahayak backend health responded locally; Reach CRM backend was not reachable.
  These are health observations, not completed business-tool integration tests.
- Human microphone confirmation and approval for any paid public audio pilot
  remain pending. No provider request or carrier call was made in these tests.

Private Unix-socket verification: **PASS** for signed readiness, status, drain and new-call rejection while draining; no audio/provider request. [Evidence](../docs/verification/voice-media-socket.json).

## Public release update — 2026-10-11

Supersedes the earlier preflight-only public deployment status above:

- Reconciled production release **81509f023a512e4e8cfa7701e93aa3260d51b3cb** deployed.
  Separate API/media containers healthy; signed media readiness passed. Production
  security/SMS baseline and independently deployed integration workers preserved.
- Public voice endpoints require authentication (401 without credentials). Internal
  media paths, console and OpenAPI remain unavailable publicly (404).
- Two temporary application identities passed authenticated create/list and
  cross-application denial checks over HTTPS; credentials revoked afterward.
- [Public TURN transport](../docs/verification/voice-public-relay.json): **PASS**,
  20 silent audio frames plus data-channel round trip with relay-only candidates.
  Initial verifier shared one TURN identity across both peers and exhausted its
  two-allocation quota; corrected to separate identities as used by production.
- Main app login remained HTTP 200 and its runtime services remained healthy.
- No paid provider request, human microphone verification, real business write,
  telephone call, invoice reconciliation or sustained production soak is claimed.

The first media-extraction/deployment milestone is implemented. Public end-to-end
speech acceptance and the remaining business-tool/handoff/billing milestones are
still open. A successful network probe is not a public-customer readiness claim.

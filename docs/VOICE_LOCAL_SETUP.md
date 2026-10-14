# Local voice platform: setup, integration and operations

Updated 2026-10-11. This extends the existing platform. The selected checkout is
`/Users/manjeet/Documents/ChatGPT/Algo/AI-Agent-Platform`.
See [acceptance evidence](../reports/FINAL_ACCEPTANCE_REPORT.md) before treating this as a public release.

## What is running

| Service | Isolated voice profile |
| --- | --- |
| Operator workspace | <http://127.0.0.1:4161/voice> |
| Platform API | <http://127.0.0.1:4160> |
| Private media service | Loopback port 4162; signed internal requests |
| PostgreSQL | Loopback port 55560 |
| Local model API | Loopback port 11435 |
| Private settings and model files | `.runtime/voice/` (ignored by Git) |

The normal `local` profile keeps ports 4060/4061/55460. Do not apply this
checkout's migrations to the newer checkout's database: readiness rejects a
database with a different migration manifest. The workspace creates a scoped
local operator identity on startup and revokes its key on orderly shutdown.
It is not a public login portal. Consuming applications own their users' login,
account recovery, memberships and backend authorization.

## Install and start

Verified host: Apple Silicon macOS, Python 3.14.7, PostgreSQL 17.11,
whisper.cpp 1.9.5, eSpeak NG 1.52.0, Ollama 0.34.3. CI configuration also
targets Python 3.12/PostgreSQL 17 on Linux; that hosted job has not run here.
Windows users need WSL2/Linux; the native launcher uses POSIX file locks.

From this repository:

```sh
python3 -m venv .venv-voice
.venv-voice/bin/python -m pip install -e '.[dev,voice,audit]'
.venv-voice/bin/python -m pip check
```

On macOS install PostgreSQL 17, whisper.cpp, eSpeak NG and Ollama using their
official distributions (Homebrew provides these). On Linux install PostgreSQL,
eSpeak NG and Ollama, and build whisper.cpp following its official instructions.
Set `PG_BIN` if the PostgreSQL tools are not on PATH. These OS dependencies
are not installed by pip.

Download a multilingual whisper.cpp model once from the project's official
model distribution. The verified pilot uses `ggml-small.bin` in
`.runtime/voice/models/`; the smaller base model failed the spoken-number
quality gate. Install a compatible local instruction model in Ollama separately.
The verified synthetic pilot used the already cached `qwen2.5:3b`.
Model licenses vary: review the actual model's license before redistribution
or a commercial rollout. Installing Ollama does not install a model.

Start an independent local inference listener in its own terminal:

```sh
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

Configure the voice profile using installed assets, then start it:

```sh
.venv-voice/bin/python scripts/voice_local_setup.py \
  --asr-model .runtime/voice/models/ggml-small.bin \
  --model qwen2.5:3b --language en
AGENT_LOCAL_PROFILE=voice .venv-voice/bin/python scripts/local.py start
```

Setup writes private files with mode 0600. It creates a separate PostgreSQL
cluster if missing, applies migrations, and starts API, workspace and task worker.
No provider credentials are copied from another checkout. `--language auto`
enables multilingual recognition; Hindi/Hinglish quality still needs human
testing. Local synthesis is intentionally basic and can sound robotic.

Open the workspace, create an agent with factual business FAQs, select it,
consent to audio processing and press **Talk to AI**. The optional transcript
checkbox controls 24-hour text retention. Microphone use needs browser
permission. The user-opened external Chrome tab now loads and connected a
microphone call. The first human attempt was interrupted repeatedly; speech-aware
VAD and a mute button have since been added. A complete human spoken reply still
needs confirmation. No browser protections were bypassed.

## Reproduce verification

With the isolated services and model listener running:

```sh
.venv-voice/bin/python scripts/verify_voice_local.py
.venv-voice/bin/python scripts/verify_voice_local.py --barge-in
.venv-voice/bin/python scripts/verify_voice_recovery.py
AGENT_TEST_CONFIG=.runtime/voice/config.json \
  .venv-voice/bin/python scripts/test_orchestration_local.py tests/python
.venv-voice/bin/ruff check src tests scripts/local.py scripts/test_orchestration_local.py \
  scripts/voice_local_setup.py scripts/verify_voice_local.py scripts/verify_voice_recovery.py
.venv-voice/bin/python -m mypy
.venv-voice/bin/python -m pip_audit --skip-editable
node --test browser-extension/*.test.mjs tests/node/*.test.mjs tests/javascript/*.test.mjs
```

The audio verifier creates a labelled synthetic clinic, sends generated speech
through a real WebRTC peer, runs real local ASR/model/TTS, transcribes returned
audio, and asserts the hours and final ENDED state. It uses no microphone,
telephone number or cloud key. It does not assert that a human liked the voice.
Results go to `docs/verification/voice-local-e2e*.json`. Each run uses one session
of the normal allowance; it does not bypass quotas. The recovery drill refuses
databases outside loopback port 55560, restores to a disposable database,
checks voice record counts and app ownership, then removes its backup/copy.

## App-facing API

Provision a separate platform service identity per consuming app using the
existing service-identity CLI. Grant only `agents:read`, `agents:write`,
`agents:run` and the required knowledge scopes. Keep the credential on the
app's server. The authenticated backend supplies `X-External-User-ID`; never
trust a browser-supplied identity. See [widget/proxy example](WEB_VOICE.md).

| Route under `/v1/voice` | Contract |
| --- | --- |
| `POST /agents` | Create, idempotent external ID, app-owned published knowledge |
| `GET /agents` | Owner's agents; paginated |
| `PUT /agents/{id}` | Full edit with `expected_version`; conflict → 409 |
| `DELETE /agents/{id}` | Pause/soft-delete; end locally owned media; keep history |
| `POST /agents/{id}/configuration` | Validated ASR/TTS/model/turn settings; increment version |
| `GET /agents/{id}/versions` | Append-only API history; paginated |
| `POST /agents/{id}/restore` | `source_version`, `expected_version`; creates a new version, rechecks knowledge access |
| `GET /agents/{id}/readiness` | Actionable configuration blockers; no proof of a successful call |
| `POST /agents/{id}/sessions` | SDP offer, unique request key, audio consent, optional transcript consent |
| `GET /sessions` | App-and-caller scoped call history, `before` cursor |
| `GET /sessions/{id}` | Durable call state plus fresh media heartbeat/connection state |
| `GET /sessions/{id}/events?after=N` | Ordered events and permitted unexpired transcripts, `next_after` cursor |
| `DELETE /sessions/{id}/transcript` | Delete text and disable future retention for this call |
| `POST /sessions/{id}/end` | End owned call; no automatic reconnect/replay |
| `GET /usage` | App allowance plus per-stage estimated API cost and uncertain reservations |

ASR/TTS choices: local, Gemini, Sarvam. Reasoning choices: local, Gemini,
NVIDIA, OpenRouter, Together. Only the local path was live-tested in this
change. Agent JSON contains model identifiers and settings, never API keys
or arbitrary model endpoints. A missing selected credential fails closed.
Every new session pins its agent version; later edits affect future sessions.
Pausing/deleting remains a live kill switch. Restoration never re-enables a
paused agent or grants access to deleted/private knowledge.

## Conversation, actions and privacy

The client streams WebRTC audio. The server detects speech, recognizes a
bounded utterance, validates a grounded model answer, and synthesizes bounded
sentences. Barge-in cancels the old turn and fences late audio. Optional local
rolling partial transcripts are provisional; they never trigger actions.
Opt-in `stream_grounded_quotes` uses SSE and speaks each complete, source-validated
quotation before model completion. It requires a model endpoint supporting streamed
JSON-schema output. Quotations retain the source language; ordinary fluent/Hinglish
answers and business actions still await complete validation. This is not native
continuous ASR or arbitrary token-to-audio speech. Local partial ASR runs at bounded
one-second checkpoints, one pending decode at a time; it is off by default.

Business operations use the existing signed app handler or consented voice
handoff contract. App handlers must enforce user authorization, argument
validation and exact-action confirmation where required. The platform provides
a unique event ID and marks uncertain operations UNKNOWN instead of replaying
them. The separate telephone-tool API already provides schema/allowlist and
subject authorization; it has not been silently granted to browser callers.

No call audio is retained. Local recognition uses a private temporary WAV
removed after each operation; use an encrypted/tmpfs temporary directory when
required. Recent conversation context is process memory only. Browser-call
knowledge answers do not create durable text or memory-learning records.
Opt-in transcripts expire after 24 hours and can be deleted early. The worker
purges expired transcripts and event rows; metadata retention defaults to 30
days (`AI_VOICE_EVENT_RETENTION_DAYS`, range 1–365). Session summaries/version
history remain for audit; a full tenant deletion process must cover these too.
Text rehearsal is a separate existing knowledge-answer path and may retain its
answer under the knowledge policy. App-handler logging is owned by that app.

## Limits, failures and recovery

- 120 seconds/call, 15 turns, two installation-wide active calls, one per caller.
- 30 starts/app/rolling 24 hours and 300 starts/app/calendar month by default.
  Monthly limit is configurable from 1–10,000. Failed attempts reserve allowance.
- Recognition input is bounded to 12 seconds; turn timeout is 5–60 seconds
  (default 45). Partial local recognition adds CPU work and defaults off.
- No fallback inside metered voice stages: a failed/cancelled provider attempt keeps
  its reserved estimate because the provider may have charged it. The separate
  legacy TTS fallback setting does not bypass this accounting boundary.
- Paid voice stages require current operator-entered rate cards and app spending
  limits before dispatch. Local stages use zero external API fees. Hosting, hardware,
  electricity, relay and carrier charges are excluded; this is not an invoice.
- Call state survives a restart. With the extracted service, API restarts preserve established media; media process loss ends audio.
  A new call is required. Never retry an uncertain business operation blindly.
- Use one media process/sticky ownership. The current pilot is not a horizontally
  autoscaled media service. Session IDs correlate the timing/outcome event trace.

Ctrl-C stops the supervised API/workspace/worker; PostgreSQL persists.
`AGENT_LOCAL_PROFILE=voice python scripts/local.py stop-db` stops only its DB.
On failure inspect readiness and the selected call's events, verify the local
model listener/assets, then restart only this profile. Do not print private
configuration. Roll back agent settings with the restore API; for schema/code
rollback restore a matching backup rather than downgrading the newer live DB.
The recovery drill verifies relational data, not external encryption-key recovery.

## Public and telephone release gates

Before public use: verify real microphones/mobile browsers, HTTPS/TURN,
restricted runtime DB roles, capacity/soak targets, backup/key recovery,
provider licenses/prices and operational monitoring. These are not proved by
the local synthetic test. Existing Asterisk/app-owned carrier integration is
separate: real mobile calls still need an authorized carrier/number. Browser
voice cannot make a SIM number receive calls through code alone.

Primary dependency references: [whisper.cpp](https://github.com/ggml-org/whisper.cpp),
[Ollama API](https://docs.ollama.com/api/openai-compatibility),
[eSpeak NG license](https://github.com/espeak-ng/espeak-ng/blob/master/COPYING).


## Restricted pilot runtime

The existing isolated profile now has a separate `voice_runtime_pilot` role.
Migrations still use `.runtime/voice/config.json`; the supervised API, workspace
and worker use the restricted credentials with:

```sh
AGENT_LOCAL_PROFILE=voice \
AGENT_LOCAL_RUNTIME_CONFIG=.runtime/voice/runtime-config.json \
  .venv-voice/bin/python scripts/local.py start
```

The runtime can read/write application tables but cannot create schemas, own
relations, create users, bypass RLS, or edit migration records, rates or limits.
This is not a claim of tenant RLS: tenant authorization remains in the API.
`scripts/voice_runtime_role.py` provisions a new deployment role from an operator
config and refuses an existing role/output. New migrations that add tables need
an operator to grant the required table privileges before restarting; do not
reuse the provisioning command as a credential-rotation command.

## Usage and spend controls

Migration 053 adds `voice_rate_cards`, `voice_spend_limits` and an append-only-in-
normal-use `voice_usage_ledger`. Each ASR/TTS/model dispatch reserves estimated
cost while holding an app row lock. Completed usage settles once; missing usage,
timeouts and cancelled calls retain their entire reservation as uncertain.
The ledger contains units and provider IDs, never audio, prompts or OTP values.

Use `scripts/voice_accounting.py --help` with an operator database connection
in the private environment. Set `rate` with provider/model/stage, currency,
per-unit amount, a verified source reference and an expiry (maximum 31 days).
Set `limits` for the app UUID, daily and monthly amounts in the same currency.
Units are audio seconds, synthesis characters and total reasoning tokens. Use
a conservative token rate covering both input/output rates and provider minimum
billing/rounding. No real provider prices are supplied by this implementation.

The usage API displays estimates and uncertain holds separately. Reviewed provider
invoice lines can now be imported as described below; this does not charge
customers, convert currency, or meter carrier charges. Historical uncertain holds
require operator investigation; do not clear them just to permit retries. UTC
day/month windows apply to monetary limits.

## Additional verification

```sh
.venv-voice/bin/python scripts/verify_voice_local.py --streaming
.venv-voice/bin/python scripts/verify_voice_local.py --streaming --barge-in
.venv-voice/bin/python scripts/verify_voice_load.py --waves 3
.venv-voice/bin/python scripts/verify_voice_deployment.py \
  --config .runtime/voice/runtime-config.json --origin https://agents.myalgodesk.in
```

The load test reserves both local call slots, uses labelled synthetic data in two
new isolated apps, verifies extra-call rejection and foreign-app read denial,
and revokes its temporary app keys afterward. Run only while no human calls are
active. Six calls are a smoke sample, not a production soak or capacity SLO.
The deployment preflight is read-only, never bypasses TLS, and intentionally
fails until separate human/carrier/relay/production acceptance is documented.

## Extracted media and baseline reconciliation

See [the authenticated media contract](VOICE_MEDIA_SERVICE.md). The launcher now requires a private `database-roles.json` with separate `agent_api`, `agent_worker` and `agent_migrator` credentials; the old single `voice_runtime_pilot` credential is not compatible with production row-level security. Use `scripts/database_roles.py` against a dedicated fresh database, then migrate as its migrator. Never point this reconciled source at the old voice-050 migration lineage. The previously saved pilot database and configuration backup remain available for recovery.

## October 11 follow-up: tools, specialists and accounting

These changes are applied to the isolated local profile through migration 066.
They are not a claim of public deployment or real telephone activation.

### Human microphone retest

The owner reported hearing only the greeting. Event metadata showed repeated
interruptions cancelling the answer. The synthetic clinic demo now has
interruptions disabled and exact FAQ streaming enabled (version 2). The local
profile uses `AI_LOCAL_TTS_ENGLISH_VOICE=en-gb`; supported English choices are
`en-us` and `en-gb`. This avoids the observed automated recognition ambiguity in
the opening-hours reply. Other agents' settings remain independent.

The same demo completed a real HTTP/WebRTC synthetic audio test with the full
nine-to-five reply. This does not confirm a human heard it. Open
`http://127.0.0.1:4161/voice`, select **Local voice demo — synthetic clinic**,
consent to audio processing, click **Talk to AI**, allow the microphone, wait for
the greeting, then ask **What are your opening hours?** End the call afterward.
Barge-in quality and mobile/public human audio still need separate acceptance.

The subsequent human call recognized a clinic-hours question but returned the
generic fallback. Local exact-quote mode now selects one bounded source-sentence
ID (or explicitly declines), and the server supplies the original text after
rechecking its source. This avoids small-model quotation rewriting. It returns
one complete sentence, not multi-source synthesis or token-by-token speech.
Three opening/closing-hours variants passed local model checks; absent pricing
and medical-advice questions were declined. Human confirmation remains pending.

### Two consuming apps

Both apps expose `POST /integrations/agent-platform/voice` using the platform's
signed business-handler contract. Bind each platform agent only to its own app
and callback secret in the private `AI_WEB_VOICE_HANDLERS` configuration. HTTPS
is required for deployed callbacks. Loopback callbacks are a local test facility.

- **Sahayak:** configure its existing calling-agent/clinic binding. The new
  internet-voice route uses the same appointment state machine, confirmation,
  event receipts, clinic isolation and uncertain-outcome handling. The isolated
  test completed one booking via the actual platform Python HTTP client. It did
  not touch real patients or send messages.
- **Reach CRM:** privately configure `AGENT_VOICE_BINDINGS` as
  `{ "<agent UUID>": {"organization_id":"<organization UUID>",
  "app_id":"<platform app UUID>", "secret":"<private shared secret>"} }`.
  Its initial handler searches approved organization knowledge and returns
  extractive FAQ answers. It does not book appointments or perform CRM writes.
  Tests cover signatures, event replay and separation between organizations.

Session creation accepts optional E.164 `caller_phone`; the widget accepts
`callerPhone` (a value or getter). This contact is pinned only to the session and
passed to its authorized app handler. It is **not verified caller identity** and
must never authorize patient-history access. Sahayak needs it to complete a
booking. The app should obtain it through its own authenticated/contact flow.

### Controlled FAQ specialists

An agent configuration can contain up to five routes:
`"specialists": [{"name":"billing", "agent_id":"<same-app agent UUID>"}]`.
Save it through the existing owner-only configuration/version endpoint.
Say **Talk to billing** to select it, or **Talk to main assistant** to return.
Names must be lowercase and unique; “main assistant” is reserved.

This only changes FAQ grounding. Every target must be enabled and visible to
the caller in the same app, with no business action handler. There are at most
three switches per session. The root session's providers, spending limits,
deadline and language stay pinned; conversation history resets at a switch.
Revoking target access prevents further turns. Handoff events contain IDs and
hop counts, not caller words. Business-handler agents do not use this FAQ routing.
This is not a human transfer or delegated authority to perform transactions.

### Reviewed provider-charge reconciliation

Migration 065 keeps actual charges separate from original runtime estimates.
Using an operator-owned private database connection, run:

```sh
.venv-voice/bin/python scripts/voice_accounting.py reconcile --file /private/path/reviewed-charges.json
```

The file is a JSON array. Each line needs `ledger_id`, `app_id`, `provider`,
`model`, `stage`, `currency`, `actual_amount`, `invoice_reference`,
`line_reference`, and the matching `provider_response_id` if recorded. Use
actual provider evidence, not model-generated prices. Runtime app roles cannot
write this table. Imports are atomic and idempotent; mismatches or changed
previously imported amounts require operator review. Active reservations cannot
be imported. Reconciliation does not change UNKNOWN execution outcomes or retry
the call. Reviewed actual amounts replace estimates/holds for budget checks.
No invoice import has been verified against a real provider bill yet.

### Monitoring and bounded soak

`GET /v1/voice/health` requires `agents:read` and reports the visible caller's
last-24-hour session counts, uncertain turns and `attention_required`. It contains
no transcripts and does not certify provider readiness or telephone operation.

```sh
.venv-voice/bin/python scripts/verify_voice_load.py --waves 60 --concurrent 1 --duration-seconds 300
```

This alternates synthetic calls between two isolated apps, checks foreign-app
session reads are denied and revokes fixture credentials afterward. It stops
on the first failed audio call. One concurrent fixture leaves the second pilot
slot available; start only when no calls are active. Local soak evidence does
not establish production concurrency, mobile networking or paid-provider limits.

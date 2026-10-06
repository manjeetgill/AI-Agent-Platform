# Asterisk calling server — local pilot

This adds our own optional SIP calling server. It does **not** attach an ordinary
Jio/Airtel SIM, enable calls to/from a mobile number, or provide a carrier trunk.
It does not use Twilio, Plivo, Sarvam, or WhatsApp calling.

## What runs

- Asterisk 20 LTS from Alpine's signed package repository, with a pinned base image
  and tested package version. Update these pins for security releases.
- One randomly password-protected SIP account (`1001`).
- Extension **600**: echo test (hear your own audio).
- Extension **700**: private AudioSocket connection to the platform bridge.
- Bridge defaults to **diagnostic** mode: one greeting beep and another beep after
  speech. These are audio plumbing tests, **not AI answers**.
- Agent mode reuses the platform's authenticated, grounded FAQ rehearsal endpoint
  and existing Gemini speech services. It can answer FAQs and follow-up questions.
  It cannot book appointments, transfer calls, send messages, or run action tools.

The main API, Research Lab, Sahayak, and existing browser voice service do not
require this compose project to be running. There are no database migrations or
changes to existing production services.

## Start on this computer

From the AI-Agent-Platform repository:

```sh
.venv/bin/python scripts/asterisk_setup.py
docker compose -f deploy/compose.asterisk.yaml up -d --build
docker compose -f deploy/compose.asterisk.yaml ps
```

The setup creates `.runtime/asterisk/` (owner-only host directory, ignored by Git).
It never overwrites existing settings. The generated SIP password is in
`sip-password`; read it privately in your editor when configuring your softphone.
Never paste it in chat, logs, source code, or a command argument.

**Mac/Colima:** UDP forwarding is now enabled with `portForwarder: grpc`.
Authenticated SIP and two-way echo audio were verified from this Mac at
`127.0.0.1:15060`. The NAT module rewrites media addresses for loopback clients;
direct peers on the private Docker voice network still receive internal addresses.

For a SIP softphone **on the same computer**:

| Setting | Value |
| --- | --- |
| Server / domain | `127.0.0.1` |
| SIP port | `15060` |
| Transport | UDP (local pilot only) |
| Username / authentication user | `1001` |
| Password | Private `sip-password` file |
| Dial | `600`, then `700` |

Docker must be running. Colima's SSH port forwarder supports TCP only. Its gRPC forwarder is required
for this UDP pilot. Changing it requires a VM restart that interrupts all local
containers; coordinate that restart and restore previously running services. No public SIP,
AMI, ARI, or AudioSocket endpoint is exposed. Do not widen the bindings to make
this a public service.

## Reproducible test without a microphone or paid calls

Keep `bridge.json` in diagnostic mode for this test. From the repository:

```sh
docker run --rm --network agent-asterisk-pilot_voice \
  --mount type=bind,src="$PWD/scripts/asterisk_smoke.py",dst=/smoke.py,readonly \
  --mount type=bind,src="$PWD/.runtime/asterisk/sip-password",dst=/run/secrets/sip_password,readonly \
  python:3.14-slim python /smoke.py
```

This sends synthetic SIP/RTP only inside Docker: authentication challenge, echo,
AudioSocket greeting and speech-response tones, hangup, and rejection of an
external-number dial string. It does not contact that telephone number.
Unit tests: `.venv/bin/pytest -q tests/python/test_asterisk_bridge.py`.

## Connect an existing FAQ agent

An operator must first select an existing voice agent and provision a dedicated,
short-lived platform service key for **its app**, with `agents:run` scope. The
`external_user_id` must be the agent's owning external user. Do not use a browser
login, caller-provided app ID, or a caller's unverified telephone number as that
identity. This pilot does not verify caller identity for private business actions.

Privately replace `.runtime/asterisk/bridge.json` with:

```json
{
  "mode": "agent",
  "platform_url": "https://YOUR-PRIVATE-PLATFORM-API-HOST",
  "agent_id": "EXISTING-VOICE-AGENT-UUID",
  "external_user_id": "AGENT-OWNER-EXTERNAL-ID",
  "service_key": "DEDICATED-APP-SERVICE-KEY",
  "gemini_key": "YOUR-EXISTING-GEMINI-KEY",
  "tts_provider": "local"
}
```

The HTTPS origin must route the authenticated `/v1/voice/agents/{id}/preview`
endpoint and be reachable from the bridge. The current public invitation-only
`agents.myalgodesk.in` proxy intentionally hides `/v1`; **it is not automatically
an appropriate API origin**. Do not expose the operator console or change that
proxy just to complete this step. Use a separately secured backend route.

```sh
docker compose -f deploy/compose.asterisk.yaml up -d --force-recreate voice-bridge
```

Call extension `700`. The agent announces that it is AI and that speech is
processed by the AI service. The agent's configured language and published FAQs
control responses, including Hindi/Hinglish. Press `#` or hang up to end.
`tts_provider: local` uses installed eSpeak NG on CPU for spoken output;
Gemini still transcribes speech and generates grounded answers. The local voice
is robotic, and Roman-script Hinglish pronunciation may be weaker than native
Hindi text. `tts_provider: gemini` uses Gemini TTS when its quota is available.
The current pilot uses local TTS because Gemini TTS returned 429 RESOURCE_EXHAUSTED.
Model access, billing/quota, latency and real microphone quality need a live test;
a successful diagnostic beep test does not verify them.

## This Mac: private connection to the existing demo agent

The existing **Hinglish receptionist — demo** is bound to extension `700`.
It contains fictional clinic FAQs; it does not represent a live clinic booking service.

- Gateway: `https://localhost:4063` (loopback; not a browser page).
- Bridge connects via `https://host.docker.internal:4063`.
- Only this agent's `preview` and `readiness` routes are available. The gateway
  checks the dedicated key and owner; the platform validates the service scope.
- A private CA is mounted only in the bridge. TLS verification remains enabled.
  No certificate is installed into macOS/browser trust stores.
- Service credential expires seven days after provisioning; metadata is in
  `.runtime/asterisk/service-key-metadata.json`. Certificates last 30 days.
- Key, Gemini credential, SIP password and TLS private key remain in the private
  `.runtime/asterisk` directory, outside Git and images.

For a new local setup with an existing console agent, run this **once**:

```sh
.venv/bin/python scripts/asterisk_connect_local.py --agent-name 'Hinglish receptionist — demo'
```

It preserves an existing agent-mode configuration instead of silently rotating
credentials. To start after a computer restart, keep these two local processes
running (separate terminals), then start the containers:

```sh
.venv/bin/python scripts/console.py
# Separate terminal:
.venv/bin/python scripts/asterisk_local_api.py
# Separate terminal:
docker compose -f deploy/compose.asterisk.yaml -f deploy/compose.asterisk.local-ai.yaml up -d
```

Do not start a duplicate process if its port is already listening. The console
and gateway must be running and this Mac must be awake for this local pilot.
Use both compose files in AI mode so the private CA remains mounted.

To test the AI call from the Mac without opening a microphone, generate a synthetic
question and use the live smoke test (this consumes existing Gemini quota):

```sh
say -o .runtime/asterisk/synthetic-question.aiff 'What are your opening hours?'
ASTERISK_HOST=127.0.0.1 ASTERISK_PORT=15060 \
SIP_SECRET_FILE=.runtime/asterisk/sip-password \
.venv/bin/python scripts/asterisk_live_smoke.py .runtime/asterisk/synthetic-question.aiff
```

Only the synthetic returned audio is saved to `.runtime/asterisk/synthetic-answer.wav`
for verification; the normal bridge does not record calls. The diagnostic `700`
test above requires diagnostic mode; it should not be used to assess an AI-mode call.

## Isolation and limits

- Single bridge process bound to **one app/agent**; no caller-selected tenant.
  For another pilot app, use a separate bridge configuration/service and a separate
  authenticated SIP context. This is not yet a shared customer onboarding system.
- Two concurrent calls, 120 seconds each, 15 turns, 30 accepted bridge calls per
  rolling 24 hours. No automatic retry of failed model requests or redial.
- Persistent SQLite call journal stores session UUID, start time and outcome;
  active calls left by a crash become `UNKNOWN`. Replay UUIDs are rejected.
  Run only one process per journal. Echo tests do not use the bridge journal.
- Raw audio and in-call history stay in memory. No call recording. The platform's
  existing FAQ answer/usage persistence still applies in agent mode.
- Speech is half-duplex: audio spoken during the AI's response is discarded.
  Silence detection is simple and needs a real acoustic test. No barge-in yet.
- AudioSocket itself is not authenticated/encrypted. It is reachable only on the
  isolated Docker voice network, never a host port. Never attach untrusted
  containers to that network. Use an authenticated encrypted transport before
  splitting Asterisk and the bridge across hosts.
- SIP/RTP is unencrypted for this local pilot; it is unsuitable for public use.
  The local username is a test extension, never an asserted telephone caller ID.
- Outbound dialing, AMI/ARI, arbitrary dialplan execution and call recording
  modules/routes are absent. Carrier credentials are not needed for these tests.

## Stop / inspect

```sh
docker compose -f deploy/compose.asterisk.yaml logs --tail 30
docker compose -f deploy/compose.asterisk.yaml stop
```

The named journal volume survives restarts. Do not delete it to bypass limits.
Secrets are never built into images. The parent directory is private on the host;
secret files are readable inside the container by its unprivileged user.

## Before actual telephone service

Still required: authorized carrier SIP trunk/number routing, verified inbound DID
and app bindings, outbound destination/consent/budget approvals and idempotent
origination, carrier-approved caller ID, secure signaling/media and network ACLs,
and an actual inbound/outbound test. Forwarding requires a real receiving number.
Asterisk software alone cannot make `7703961610` (or another SIM) API-accessible.
Booking also requires the signed app adapter with caller/session authentication;
this FAQ-only pilot deliberately does not invoke it.

Protocol reference: [Asterisk AudioSocket](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/).

## Verified locally on 2026-10-06

- 18 focused tests passed, including strict TLS chain validation, gateway route
  and credential isolation, call limits, replay protection, and local TTS selection.
- Mac loopback SIP authentication and bidirectional RTP echo passed.
- Live AI call to extension `700` passed using synthetic speech: greeting, opening
  hours question, Hinglish answer, and acknowledged hangup. Journal state `ENDED`.
- Returned 7.6-second audio was transcribed back and matched the fictional FAQ:
  Monday–Saturday 9 AM–5 PM, Sunday closed.
- The existing voice workspace and Sahayak returned HTTP 200 after the VM restart.
- A real microphone/softphone UI test is still needed for acoustic quality. No
  mobile/PSTN call was placed and no real appointment was created.

## Reuse with multiple apps

The bridge now also accepts a connection-bound `connection_id`/`gateway_key`
configuration. App-specific admission, routing, limits and call history then use
`/v1/telephony`. See [App-owned calling](APP_OWNED_CALLING.md). Existing one-agent
local configurations still work for rehearsal; they do not verify a telephone number.

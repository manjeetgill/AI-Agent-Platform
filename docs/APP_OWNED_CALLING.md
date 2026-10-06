# App-owned telephone connections

## Responsibility and current scope

Each consuming app owns its carrier account, telephone numbers, carrier authorization,
SIP trunk and call charges. Our platform supplies the scoped API, number-to-agent
binding, speech processing, bounded AI conversations, permitted business tools, audit and usage records. The supplied Asterisk
AudioSocket bridge runs beside the app's Asterisk, on a private network. Deploy one
bridge and separate credentials/journal per connection; do not share AudioSocket
networks between customers. The app routes only the authorized DID to that bridge.

This is an **inbound voice and business-tool adapter** with English, Hindi and Hinglish support.
It answers FAQs and can check/book appointments through an app-provided tool endpoint.
It does not originate calls, transfer calls, connect arbitrary SIMs, provision a carrier, or authorize caller ID. Caller
telephone identity is not used to authorize business actions. No Twilio or Plivo dependency is required by this adapter. Sarvam speech is now an optional transport; see below.

## Setup for another app

1. The platform operator provisions the app's existing service identity with
   `telephony:read`, `telephony:write`, and the voice/knowledge scopes needed to
   create its agents and FAQs. Keys stay on the app backend. The backend maps
   signed-in business owners to stable `X-External-User-ID` values. Never accept
   that identity directly from a public form without authenticating the user.
2. Create an agent and published business FAQs using the existing `/v1/voice/agents`
   and knowledge APIs. Create the connection through `POST /v1/telephony/connections`
   with `{external_id, agent_id, number}` (E.164, e.g. `+917703961610`). The response
   gives `id` and a **one-time gateway_key**. Save it in the app's secret manager.
   Reusing an external ID returns 409, never another secret. List to reconcile an
   uncertain create; if the key was lost, use the explicit rotate-key operation.
3. The app configures its authorized carrier route and its private bridge. Carrier
   passwords stay there; our API neither requests nor stores them. Gateway keys
   are stored only as SHA-256 hashes. The gateway sends a heartbeat every 30 seconds.
4. Have the platform operator inspect the carrier's authorization and DID routing
   evidence. There is currently no automated carrier ownership lookup. The operator
   records an opaque evidence/ticket reference using the CLI below. This is a manual
   onboarding gate; an app cannot assert `verified: true` or claim another app's number.
5. Read `/connections/{id}/readiness`, resolve blockers, then PATCH the connection
   with `{enabled: true}`. This only admits test calls; **a successful live inbound
   call is still required before rollout**. Readiness does not claim that speech
   quotas, carrier connectivity or acoustic quality have been proven.
6. Test a real authorized incoming call: hear AI disclosure/greeting, ask an FAQ,
   hear the correct answer, end, and inspect the call record. Test two different apps
   independently before production. No real telephone call is part of automated tests.

A backend client is in `examples/telephony-integration/client.py`. Operator UI:
`http://127.0.0.1:4061/telephony`, linked from Voice agents. This localhost console
uses its own operator app identity; it is not an end-user login or public dashboard.

## Private bridge configuration

Use the existing bridge image (`deploy/asterisk/Bridge.Dockerfile`) with a secret
file mounted at `AI_ASTERISK_CONFIG`. Example structure (placeholders only):

```json
{
  "mode": "agent",
  "platform_url": "https://your-platform-api.example",
  "connection_id": "connection-uuid-from-the-api",
  "gateway_key": "tgw_private-value-from-the-api",
  "number": "+917703961610"
}
```

The gateway needs **no Gemini key**. It sends short audio turns to the platform,
which handles recognition, conversation, tools and speech output. Configure
`AI_CALLING_AUDIO_API_KEY` (or the existing Gemini fallback credential) privately
on the platform. `AI_TELEPHONY_TTS_PROVIDER=local` uses espeak-ng on the platform;
otherwise Gemini TTS is used. Build the API image with `INSTALL_VOICE=1` for local
CPU speech support. AI and hosting costs remain separate from carrier charges.
Asterisk AudioSocket protocol
UUIDs are session identifiers, not authentication: never publish port 9092. Use
carrier-approved SIP/TLS/media security and ACLs on the app's telephone gateway.
The localhost Compose pilot is not a production SIP trunk configuration.

Unlike the legacy one-agent pilot config, a connection-bound bridge needs no app
service key, user ID or selectable agent ID. The platform selects those from the
verified connection. Call admission and response history are stored centrally.

## API contract

All management paths start with `/v1/telephony`, require a scoped app bearer key
and the authenticated owner's `X-External-User-ID`:

| Method/path | Purpose |
| --- | --- |
| GET `/capabilities` | Exact supported functionality |
| POST, GET `/connections` | Prepare / list own connections (limit/offset pagination) |
| GET `/connections/{id}/readiness` | Blockers, current state and app limits |
| PATCH `/connections/{id}` | Activate or pause |
| POST `/connections/{id}/rotate-key` | Issue a replacement gateway key; pauses and invalidates old key |
| GET `/connections/{id}/calls` | Paginated state, turns and timestamps; no raw audio |
| GET `/connections/{id}/audit?after=0` | Cursor-based setup/verification/activation audit |

Gateway requests use only the connection's bearer `tgw_...` key:

| Method/path under `/v1/telephony/gateway/{connection_id}` | Body |
| --- | --- |
| POST `/heartbeat` | none |
| POST `/calls` | `{call_id: UUID, number: configured_DID}` |
| GET `/calls/{call_id}` | none; reconcile uncertain admission |
| POST `/calls/{call_id}/turns/{1..15}` | `{text: transcribed_utterance}` |
| POST `/calls/{call_id}/end` | `{state: ENDED or FAILED or UNKNOWN}` |

All gateways for one app share an atomic admission quota (default 2 concurrent,
100 starts/rolling 24h). Each call lasts at most 120 seconds and 15 turns. The
existing bridge also retains its stricter local 30 starts/day and two-call limit.
Admission reserves before work. Duplicate calls are rejected; expired sessions
appear UNKNOWN and release capacity. Completed duplicate turns return the saved
reply only for unchanged input. Pending/unknown turns never invoke the model again.
Conversation history is built on the server, not accepted from the gateway.
Pause, key rotation, carrier revocation and agent disable block further answers,
including results still being generated. End is idempotent and never overwrites a
terminal state. No raw audio is persisted here. An app-verified subject identifier is stored when the app authorizes a protected call. Transcribed
utterances/replies **are** stored; apply the app's disclosure and retention policy.

## Operator administration

Run with the correct private `AI_PLATFORM_DATABASE_URL` environment (never put
credentials in shell arguments). Migrate before starting the updated API:

```sh
python -m agent_platform.migrate
python scripts/telephony_admin.py verify CONNECTION_UUID --operator OPERATOR_ID --evidence-reference CARRIER_TICKET
python scripts/telephony_admin.py revoke CONNECTION_UUID --operator OPERATOR_ID --evidence-reference REVOCATION_TICKET
python scripts/telephony_admin.py limits APP_UUID --concurrent 2 --daily 100
python scripts/telephony_admin.py purge --older-than-days 30
```

Verification always pauses the connection; reactivation requires a live gateway.
Verified numbers are globally unique. Transferring a number requires revoking the
old binding, checking new authorization, and verifying the new connection. Gateway
key rotation pauses existing calls; update the private bridge secret and reactivate.
Schedule the purge command with your deployment's retention jobs; it is not yet
scheduled automatically. Turn rows cascade with deleted calls. Existing knowledge
answer records have their own lifecycle. Do not log credentials or request bodies.

## Deployment and practical limits

API/database workers remain independent of the optional calling process. Deploy
migrations 027 and 028 plus updated API and bridge. Remote apps/gateways need HTTPS access
to these authenticated endpoints. The updated `deploy/agents.Caddyfile` exposes
only the scoped telephony, voice-agent and knowledge-document routes on agents.myalgodesk.in
and permits the bounded speech body size. Apply it together with the updated API;
the deployed server may still be running the previous configuration.
Keep the local operator UI private, and configure edge request limits. Existing
WhatsApp routes must remain intact. Do not expose AudioSocket as a public webhook.

Automated tests verify two-app isolation, owner scoping, number gating, concurrent
quota races, turn idempotency, timeouts, revocation and Asterisk adapter requests.
They do not establish carrier access, production capacity for hundreds of apps,
provider billing, or live PSTN audio. Those need deployment and load/live-call tests.


## Booking tools: completing the business-app flow

The app developer implements one signed HTTPS endpoint; see the runnable fictional
`examples/telephony-integration/booking_app.py` and `booking-tools.json`. The example
stores bookings and idempotency results atomically. Replace its demo rules with the
app's real scheduling transaction, clinic permissions and slot availability checks.
It is a reference adapter, not a live Sahayak clinic connection.

1. The operator allows the business app's exact HTTPS origin using private platform
   config `AI_TELEPHONY_TOOL_ORIGINS={"APP_UUID":["https://app.example"]}`. Only allow
   app-controlled origins with stable approved routing. App users cannot change
   this allowlist. Apply egress firewall restrictions in production. Requests never
   follow redirects or use environment proxies.
2. Configure the existing AES-GCM keyring (`AI_META_ENCRYPTION_KEYS` and
   `AI_META_ENCRYPTION_KEY_ID`) on the platform. The shared utility's historic name
   does not introduce a Meta dependency. Tool signing secrets are encrypted with
   app/owner/connection-bound authenticated data; no read API returns them.
3. Your backend PUTs `/connections/{id}/tools` with `{endpoint, secret, tools}`.
   Each tool has `name`, `label`, `description`, `risk`, `requires_subject` and typed
   `fields`. See the JSON example. Tool definitions are grants from the app backend,
   not from the model. Register only narrow operations and re-check authorization
   in the app endpoint. Declare money/payment/secret operations as `USER_ONLY`.
4. `READ` tools such as public slot availability can execute immediately when granted.
   `WRITE` tools persist exact arguments and a deterministic spoken summary. Only a
   subsequent exact yes/haan/हाँ executes that saved action; edits or ambiguous yes
   do not. `USER_ONLY` tools are never dispatched. Names/arguments/config version,
   app, call, subject and authorization revision are pinned in the action hash.
5. Protected tools default to `requires_subject: true`. After your own backend has
   verified the customer **and their association with this call**, POST
   `/connections/{id}/calls/{call_id}/authorize` with `{subject, tools}`. This accepts
   only the owner-scoped service credential, never the gateway key. Caller ID, speech
   and model output cannot authorize an identity. Empty grants revoke access; changes
   cancel pending approvals. For a public new-booking operation your app may explicitly
   choose `requires_subject: false` and handle guest validation itself. Existing records,
   cancellations and account-sensitive actions should stay protected.
6. Every tool POST includes an action ID, action hash, app/owner/session, tool, arguments,
   subject trust and confirmation turn. Verify HMAC SHA-256 over `timestamp + "." + body`
   using `X-Agent-Timestamp`/`X-Agent-Signature`, reject timestamps outside 60 seconds,
   verify app/owner/subject entitlements, and deduplicate atomically by action ID/hash.
   Return `{action_id, action_hash, outcome: "applied", text}` only after commit.
   `text` is spoken verbatim (maximum 1,200 characters); never include secrets.
7. An uncertain transport/model result is never automatically resubmitted. Inspect
   GET `/connections/{id}/calls/{call_id}/actions`. POST the action's `/reconcile`
   path to query the same endpoint with `operation: "reconcile"`, even after hangup.
   Reconciliation must **never execute** the action. Return `applied`, `unknown`, or
   `not_applied`. A `not_applied` response also requires `fenced: true`: atomically
   tombstone that action ID so delayed execution cannot happen afterward. The example
   demonstrates this. Only a new, separately confirmed action can then be attempted.
8. GET the call's `/usage` for planner tokens and speech durations. RAG usage remains
   in the existing knowledge ledger. These are usage records, not a carrier invoice.

Tool calls are bounded to one per turn with an eight-second endpoint timeout. The
platform reserves dispatch before the network request. Configuration changes revoke
pending proposals; permission changes are checked again immediately before dispatch.
Already dispatched work may finish when a caller hangs up, so the app must expose
its saved outcome for reconciliation. Configuration replacement can prevent automated
reconciliation of older actions; preserve the old app action ledger for operator review.

### Audio transport

POST `/gateway/{id}/calls/{call_id}/speech/0` for the disclosure/greeting. POST turns
1–15 with `Content-Type: application/octet-stream`, mono 16-bit little-endian PCM
at 16 kHz, maximum 12 seconds / 384,000 bytes. Receive WAV plus `X-Voice-Action`.
Allow at least 384 KB at the HTTPS edge and up to 65 seconds for a complete speech
turn; the call's overall 120-second deadline still applies. The supplied bridge now
uses this route automatically for connection-bound calls. Each turn is reserved once
before speech services run; replay is rejected, no audio is stored, and errors remain
visible in the call/action records. The legacy one-agent rehearsal mode is unchanged.

### Local verification

`test_telephony_flow.py` tests signed app requests, availability → identity → exact
confirmation → one booking, lost responses after commit and reconciliation, revoked
grants, blocked user-only steps, and audio-in → booking → audio-out. It uses synthetic
speech providers and a fictional app database, not a real carrier or patient record.


The operator setup page also accepts the app endpoint, private signing secret and
JSON tool catalogue. App developers normally configure these through the backend
client. `scripts/telephony_model_smoke.py` checks real-model tool selection with
fictional data and never executes business actions.


## Optional Sarvam speech adapter (local implementation, not live-tested)

`AI_CALLING_SPEECH_PROVIDER=sarvam` selects Sarvam REST speech recognition and
synthesis for the existing platform voice pipeline. Store `AI_SARVAM_API_KEY`
privately on the API/voice workers. STT uses saaras:v3 codemix for Hindi/English;
TTS uses bulbul:v3. Requests are bounded, redirects disabled, response sizes
limited, and provider failures are not automatically retried or silently routed
to another paid speech provider. Model reasoning still uses the existing separate
Gemini configuration; Sarvam keys never go to Gemini endpoints. No audio persists
in this adapter. Existing signed app business tools and their approval gates remain.

This provides speech I/O only. It does not create Sarvam managed deployments,
rent a phone number or connect a SIM. A Sarvam managed-calling adapter and live
telephone setup remain pending account access and the selected telephony path.
No live Sarvam API request or telephone call has been tested in this revision.
Verify eligibility, KYC, available credits and number fees before provisioning.
Do not claim two-app calling completion from speech transport tests.

References checked 2026-10-06:
- https://docs.sarvam.ai/api-reference/speech-to-text/transcribe
- https://docs.sarvam.ai/api-reference/text-to-speech/convert
- https://docs.sarvam.ai/conversations/deploy/telephony/rent-from-sarvam

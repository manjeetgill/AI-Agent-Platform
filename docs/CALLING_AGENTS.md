# Calling agents: inbound foundation

Status: inbound foundation deployed to the existing pilot server on 5 October 2026;
signed HTTPS Sahayak readiness verified, but no live phone call has been tested. Calling
is independent of WhatsApp; connecting a WhatsApp number does not authorize its use
for telephone calls. No number was purchased and no provider account was changed.

## What an integrating app gets

The `/v1/calling` API uses existing backend service credentials with `channels:read`
and `channels:write`, plus `X-External-User-ID`. Agents, configuration, history and
activation are isolated by platform app and owner. A business workspace can use its
existing channel scopes. Keep credentials on the app backend, never in browser code.

- GET `/v1/calling/capabilities`: supported features and operator binding identity.
- POST `/v1/calling/agents`: save a draft or operator-bound agent. `external_id`
  makes identical creates idempotent; changed content returns 409.
- GET `/v1/calling/agents`: paginated list (`before`, `next_before`).
- PUT `/v1/calling/agents/{id}`: change settings while paused with no active calls.
- GET `/v1/calling/agents/{id}/setup`: redacted readiness and provider callback URLs.
- POST `/v1/calling/agents/{id}/preview`: configuration preview; no phone/app calls.
- POST `/v1/calling/agents/{id}/activation`: `{ "enabled": true/false }`. Enabling
  requires operator-authorized provider credentials and a signed app readiness probe.
- GET `/v1/calling/agents/{id}/calls`: paginated states, turns and reported duration.

Create payload:

```json
{
  "external_id": "clinic-reception",
  "name": "Clinic receptionist",
  "connection_ref": null,
  "language": "en-IN",
  "greeting": "Hello. I am the clinic's automated assistant. How can I help?",
  "handoff_message": "I cannot complete that safely. Please contact reception.",
  "max_turns": 15
}
```

`connection_ref: null` saves a draft. Languages currently accepted: en-IN, en-US,
hi-IN. After the operator connects an authorized voice-capable number, update the
draft with its connection reference. An operator connection reference cannot be assigned to two
agents; keep one registry entry per authorized provider number. Do not advertise customer signup as automatic: provider verification and
number provisioning remain operator steps in this version.

## One-time operator setup

Apply migration 022; install dependencies including the pinned official Twilio SDK.
Store `AI_CALLING_CONNECTIONS` in the backend's private secret configuration:

```json
{
  "clinic-voice": {
    "provider": "twilio",
    "app_id": "APP_UUID_FROM_CAPABILITIES",
    "owner_id": "OWNER_UUID_FROM_CAPABILITIES",
    "number": "+COUNTRYCODE_NUMBER",
    "number_verified": true,
    "account_sid": "AC_ACCOUNT_IDENTIFIER",
    "auth_token": "PRIVATE_PROVIDER_AUTH_TOKEN",
    "public_url": "https://agents.example.com",
    "handler_url": "https://your-app.example.com/integrations/calling",
    "handler_secret": "PRIVATE_RANDOM_SECRET_AT_LEAST_32_CHARACTERS"
  }
}
```

Only set `number_verified` after independently checking number authorization and
provider ownership. This is an operator attestation, not automatic provider proof.
The registry is server-only and binds both app and owner. Restart API after changes.
Expose only POST `/webhooks/calling/{id}/inbound`, `/turn/{turn}`, and `/status` on
HTTPS. Do not expose the private management API. The pilot proxy exposes signed calling callbacks; unknown or unconfigured agents
cannot execute calls. Private management endpoints remain inaccessible publicly.

Copy the `setup` response URLs to the provider's number configuration: inbound Voice
webhook and call-status callback, both POST. Then enable and place a real inbound
call. Provider signature checks use the configured public origin, not an untrusted
Host/forwarded header. Wrong account, destination and outbound-call requests fail.
Recording and outbound dialing are not implemented. Provider charges still apply
to real inbound calls; configure account spending controls before activation.

## The connected app owns business actions

Twilio handles speech recognition and speech synthesis. The platform converts one
speech/keypad result into one signed request to your app. Your app can use its AI
agent, knowledge and tools to decide the response. **This release does not implement
a second general AI reasoning loop inside calling.** Sahayak now implements
the signed booking adapter and owns the restricted appointment conversation.

Signature: HMAC-SHA256 over `X-Agent-Timestamp + '.' + raw request bytes`, in hex in
`X-Agent-Signature`. Verify within five minutes, validate `app_id`/`agent_id` against
your saved business binding, and deduplicate `event_id` durably. A probe has
`probe: true`, `event_id`, `agent_id`, `app_id`; respond exactly
`{"ready":true,"event_id":"SAME_UUID"}` without business writes.

A real turn adds:

```json
{
  "probe": false,
  "event_id": "UUID",
  "call_id": "UUID",
  "agent_id": "UUID",
  "app_id": "UUID",
  "caller": "+CALLER_NUMBER",
  "input": "I would like an appointment",
  "input_trust": "untrusted_caller_data",
  "turn": 1,
  "language": "en-IN"
}
```

Respond within five seconds:

```json
{"event_id":"SAME_UUID","action":"continue","text":"Is this your first visit?"}
```

Actions: `continue`, `end`, `handoff`. Text is XML-escaped; it cannot inject Dial,
Record, URLs or other telephony commands. `handoff` currently records the need for
human help, speaks your message and ends the call. **It does not transfer the live
call to a person.** Your app must create its staff follow-up task if needed.

The app must enforce caller consent, caller identity for private data, action
permissions and explicit confirmation before a booking or financial commitment.
Caller ID and transcribed speech are not authentication. Never treat speech as
system instructions. Sahayak should retain its own schedules, appointments,
patient consent, medical-safety checks and confirmation policy.

## Failure and storage behavior

- A turn is committed as PROCESSING before invoking the app. Repeated delivery
  reuses its saved XML. Changed input for an existing turn returns 409.
- A timeout or invalid response becomes UNKNOWN with the safe fallback. No automatic
  repeat of a possibly completed business action. A crash leaves PROCESSING for
  operator reconciliation; replay stops safely and never runs that turn again.
- Pause prevents subsequent new turns; an already running app action cannot be
  recalled. Completed provider callbacks cannot be reopened by delayed turns.
- At most 30 turns, a 15-minute turn-admission window, bounded input and five-second
  app HTTP timeout. These are application safeguards, not a provider billing cap or
  guaranteed hard call-duration cutoff.
- Caller number, state, response XML and request hashes are durable. Raw speech,
  audio recordings and transcripts are not stored by this runtime. Response text
  can still contain personal data. Set a retention/erasure policy before customer
  launch; automated retention and billing reconciliation are not implemented yet.
- Duration is provider-reported usage, not a calculated invoice. Call history omits
  caller numbers. Audit records contain setup/activation events, not utterances.

## Tests and next gates

Tests exercise the real API and PostgreSQL with simulated provider callbacks and
app handlers: owner isolation, signature checks, XML injection, duplicate-turn
fencing, uncertain results, activation, pause and out-of-order status updates.
No claims of actual phone delivery or speech quality follow from these tests.

Remaining: authorized provider account/voice number, permanent Sahayak deployment,
live call validation, customer voice onboarding, live transfers,
recording consent/retention, outbound approvals and production metering/quotas.

Provider references:
- https://www.twilio.com/docs/usage/security
- https://www.twilio.com/docs/voice/twiml/gather
- https://www.twilio.com/docs/voice/twiml/say

## Sahayak pilot deployment

The inactive clinic draft is `418c8845-97b3-4346-983b-1acc1a2c4731`.
The operator requested **+91 77039 61610**. This is an intended number only;
WhatsApp messaging authorization is not proof of PSTN voice routing. Do not set
`number_verified` or activate until the chosen voice provider confirms routing
and authorization. WhatsApp voice requires its own media adapter, which is not
implemented by this Twilio HTTP turn adapter.

The pilot handler is
`https://shayak.myalgodesk.in/integrations/agent-platform/calling`.
It uses the existing signed relay to the local Sahayak API. The laptop and SSH
tunnel must remain running; it is not a permanent deployment of Sahayak.

Migration 022 and the Twilio SDK are installed. The pilot image was built on the
running image with only the calling module, dependency and app registration
added, preserving the deployed WhatsApp behavior. Runtime tag:
`agent-platform:calling-pilot`; previous image: `agent-platform:before-calling`.
The normal source Dockerfile includes the same dependency for subsequent builds.

Verified: signed public readiness returns 200, unsigned requests fail, main app
login returns 200 and unauthenticated main API returns 401, private calling
management remains 404 publicly. No outbound call was made, no number purchased,
and no recording/live transfer was enabled. A real call remains the final gate.

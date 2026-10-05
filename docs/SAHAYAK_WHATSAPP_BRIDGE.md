# Sahayak WhatsApp booking bridge

The platform owns WhatsApp authentication, signed provider webhooks, durable message jobs and outbound dispatch fences. Sahayak owns clinic information, patient consent/safety, conversation state and appointment writes. A SAHAYAK channel never falls back to RAG when its clinic endpoint fails.

## Configure (operator only)

1. Deploy platform migration 020 and Sahayak migration 019. Deploy both updated services.
2. Provision a separate platform business workspace for the clinic; connect its authorized business number as usual.
3. Create a disabled channel with `execution_mode: "SAHAYAK"`, `documents: []`, `name` and `phone_id` using `POST /v1/whatsapp/channels`. To change an existing channel, pause it, reconcile outstanding jobs, then `PUT /v1/whatsapp/channels/{id}/workflow` with `{"execution_mode":"SAHAYAK"}`. This does not activate it.
4. Generate one private random secret of at least 32 characters per binding. Configure the same secret on both backends. Do not put it in browser code or clinic forms.

Platform `AI_SAHAYAK_BRIDGES` is JSON keyed by channel UUID. Each value contains:

```json
{"app_id":"CLINIC_PLATFORM_APP_UUID","clinic_id":"SAHAYAK_CLINIC_UUID","url":"https://CLINIC_API/integrations/agent-platform/whatsapp","secret":"PRIVATE_SHARED_SECRET"}
```

Sahayak `AGENT_CHANNEL_BRIDGE_BINDINGS` is JSON keyed by the same channel UUID:

```json
{"appId":"CLINIC_PLATFORM_APP_UUID","clinicId":"SAHAYAK_CLINIC_UUID","secret":"PRIVATE_SHARED_SECRET"}
```

The endpoint must be reachable from the platform worker. Local loopback HTTP is allowed for development; a public deployment requires HTTPS. Publish only the required endpoint through the reverse proxy. Provider credentials remain in the platform. Restart services after changing environment secrets.

5. Verify the provider webhook and worker health. Activation performs a signed, non-mutating probe of Sahayak and checks the exact clinic binding. Enable with the existing channel activation API only after the owner-authorized test succeeds.

## Processing and failure behavior

- HMAC-SHA256 signs `timestamp + '.' + exact UTF-8 request bytes`. Sahayak rejects signatures older than five minutes and mismatched clinic/app/channel IDs.
- The event ID is the platform's durable message UUID. Sahayak commits a PROCESSING receipt before calling its booking orchestrator and serializes concurrent turns for the same clinic/patient.
- A DONE duplicate returns the saved response. Altered content for an event is rejected. PROCESSING/UNKNOWN duplicates cannot execute booking again; the operator must reconcile the receipt, appointment and platform message before recovery.
- Replies are stored in Sahayak but captured without its independent WhatsApp sender/outbox. Only the platform dispatches the combined reply, once, through its existing provider fence.
- A clinic handoff/emergency notice is sent once, then the platform conversation becomes HUMAN. For resumption both the Sahayak conversation and platform conversation must be explicitly returned to automation by staff. Staff reply transport from Sahayak's inbox is separate; use the platform inbox for delivery until that route is integrated.
- STOP is forwarded to Sahayak to persist consent withdrawal and cancels older queued automatic replies. Generic channels retain their existing handling.
- Receipt responses are scrubbed by existing patient erasure/retention routines; deduplication tombstones remain. Platform conversation history has its own retention obligations.
- This bridge is for inbound text booking and immediate replies. Attachments, proactive reminders, calling and native interactive buttons are not enabled by it.

## Validation performed

The Sahayak bridge test invokes this repository's actual Python bridge client against a local HTTP server, completes the appointment menu, and replays final YES without a second booking. Run with `AI_AGENT_PLATFORM_DIR=/path/to/AI-Agent-Platform pnpm exec vitest run apps/api/test/whatsapp-bridge.test.ts` from Sahayak. Other tests cover signatures, replay, tenant isolation and unfinished receipts. Platform tests use a disposable migrated database and a fake WhatsApp sender to check dispatch fencing, handoff delivery, missing-bridge failure and activation gating. These are not proof of delivery to a real phone.

## Integrated Sahayak setup (preferred)

The wizard now supports Save and connect, resuming the same authorization, automatic preparation after operator ownership verification, webhook-test status, and enable/pause. The channel is created idempotently from the verified signup session. An unverified browser callback cannot create a channel.

One-time operator settings on Sahayak:
- AGENT_CHANNEL_PLATFORM_URL and AGENT_CHANNEL_CREDENTIALS (clinic-scoped service identity).
- AGENT_CHANNEL_BRIDGE_URL: the Sahayak signed bridge endpoint.
- AGENT_CHANNEL_BRIDGE_SECRET: a private random root secret of at least 32 characters. Sahayak derives a separate secret for each clinic/app/channel binding.

One-time operator settings on the platform:
- Existing Chakra partner signup settings and public webhook address.
- Existing Meta credential encryption keyring (also encrypts clinic bridge bindings).
- AI_SAHAYAK_ALLOWED_ORIGINS: JSON array of approved Sahayak backend origins. Local testing may explicitly allow http://127.0.0.1:4000; production uses HTTPS.

After verification, Sahayak obtains the deterministic channel, records its clinic association, registers the derived encrypted bridge configuration, and configures the Chakra webhook. No per-channel environment editing is needed. Existing configured channels cannot be reassigned to a different clinic by repeating setup. Enable still checks provider webhook verification, worker heartbeat and a signed Sahayak probe. A second-number test is needed before customer use.

Apply platform migration 021 and Sahayak migration 020 for this wizard. The UI distinguishes missing provider authorization setup from missing clinic details. A configured local service credential alone does not make new-number signup available: Chakra partner settings and a public webhook URL must also be supplied.

# Inbound WhatsApp support

This is an opt-in platform capability, independent of NRAIAlgo. It uses Meta's
official Cloud API. No phone account has been connected and no live message was
sent during development. Calling agents and public multi-business onboarding are separate
phases. The local operator setup wizard and inbox are available at `/whatsapp`. This release supports text questions answered independently from selected
knowledge documents; transcripts are saved but are not supplied as model memory.

## Use the workspace

Run `python scripts/console.py` and open http://127.0.0.1:4061/whatsapp.
The local launcher applies migrations and starts both the task and WhatsApp workers.

1. Expand **Add or update a Meta connection**. Enter the Meta phone-number ID,
   supported Graph API version, access token, app secret and your chosen verify token.
   The local form writes `.runtime/local/whatsapp-connections.json` atomically with
   mode 0600. Secret inputs are cleared after submission, not saved to browser storage,
   and never returned by the API. This is a private local file, not an encrypted cloud vault.
2. Publish customer-facing FAQs, then select a connected number and explicit documents
   to create an inactive agent.
3. Deploy the platform on public HTTPS (or use an operator-managed development HTTPS
   tunnel), configure Meta's displayed callback, verify the challenge and subscribe to
   messages. `AI_PLATFORM_PUBLIC_URL` controls the displayed callback host; setting it
   alone does not create hosting, DNS or TLS.
4. Refresh the checklist. Activation requires successful webhook verification and a
   recent worker heartbeat with a configured model. Configuration is not proof of model
   access or Meta token validity: verify actual delivery with a test number first.
5. Open **Conversation inbox**, choose an agent and conversation. Take over, type an
   exact reply and press **Send WhatsApp reply**. Refresh to see provider acceptance
   and delivery. Resume enables automatic answers to new messages only.

The screens are intentionally loopback-only operator tools. Do not expose this console
publicly. For end users, embed the same management APIs behind your application's
sign-in and per-user server-side identity; use a production secret manager. Managed
Meta Embedded Signup is implemented in the connection service described below. Live customer onboarding is not yet verified; Meta platform configuration and publication remain required.

Changing local connection credentials pauses the channel and clears webhook verification.
The console does not buy numbers, approve templates or configure Meta subscriptions.
A customer must message the business first; proactive/campaign sends are outside this release.

## API setup

1. Apply migrations (`python -m agent_platform.migrate`).
2. Provision a platform app service key with `channels:read`, `channels:write`,
   `knowledge:read`, `knowledge:write`, and `knowledge:publish`. Keep keys server-side.
3. Upload customer-facing text documents using `/v1/knowledge/documents` with
   `visibility: "app"`. Only documents explicitly selected for the channel are used.
   Internal/private documents must not be selected or published. Retrieval currently
   uses the existing English lexical search, not multilingual semantic search.
4. The operator provisions `AI_WHATSAPP_CONNECTIONS` in the API and WhatsApp worker's
   secret environment. JSON shape (placeholders only):

   ```json
   {"META_PHONE_NUMBER_ID": {"app_id":"PLATFORM_APP_UUID",
     "access_token":"SECRET", "app_secret":"SECRET", "verify_token":"SECRET",
     "graph_version":"SUPPORTED_META_GRAPH_VERSION"}}
   ```

   The phone-number ID is bound to an app by the operator. An API caller cannot
   substitute another app's connection. Use a currently supported Graph API version
   for the business's Meta app. Meta account/number verification and permissions
   must be completed separately. Production should inject this environment from a
   secret manager; tokens are never persisted in channel rows or returned by APIs.
5. `POST /v1/whatsapp/channels` with `name`, `phone_id`, and `documents` (UUID array).
   Channels are created disabled; webhook verification and a configured running worker are required to activate. Management APIs require a service bearer key and
   `X-External-User-ID`, supplied by the consuming app's authenticated backend.
6. Configure Meta's HTTPS webhook to `/webhooks/whatsapp/CHANNEL_UUID`, using the
   provisioned verify token, and subscribe to messages. This public endpoint checks
   the raw request body's HMAC signature and the configured phone-number ID. Expose
   only required public webhook paths; keep management APIs protected.
7. Explicitly enable automatic customer replies with
   `POST /v1/whatsapp/channels/CHANNEL_UUID/activation`, `{"enabled":true}`.
8. Start `python -m agent_platform.whatsapp_worker`, or
   `docker compose --profile whatsapp up -d`. Use one WhatsApp worker for the pilot.
   Existing model configuration and daily generation quotas apply.

Use Meta's test number and approved test recipients first. Verify a real inbound
question, one answer, human takeover, and provider delivery receipts before rollout.
Model usage and any provider charges require the business's activation/budget policy.

## Inbox and handover API

All management resources are scoped to both app and operator owner identity.

- `GET /v1/whatsapp/channels`
- `GET /v1/whatsapp/channels/{id}/conversations`
- `GET /v1/whatsapp/channels/{id}/conversations/{conversation_id}`: message history,
  reply, citations, delivery state and processing outcome.
- `POST .../conversations/{conversation_id}/mode`, `{"mode":"HUMAN"}`: cancel queued
  automatic work. `{"mode":"AUTO"}` resumes on new questions only, never replays old ones.
- `POST /v1/whatsapp/channels/{id}/messages/{message_id}/reply`, `{"text":"Exact human reply"}`:
  queue a human's reviewed response in HUMAN mode. Retrying the same text returns its
  saved state. Already sent/uncertain replies cannot be overwritten or resent.
- Disable the channel using `activation` with `enabled:false`.

Lists accept `limit` and `before` (last returned UUID); UUID order is stable pagination,
not chronological. Sort returned message timestamps for an inbox display.
Messages "human", "agent", "stop", and "unsubscribe", unsupported media, oversized
questions, or insufficient evidence require human handling. There is no automatic
acknowledgement on handover in this release; the inbox records it. The workspace inbox surfaces the handover queue to staff; external staff notifications are not yet connected.

## Durability and safety

Webhook intake and the work item share a transaction. Provider message IDs deduplicate
retries. Bounded model generation is claimed atomically; uncertain generation is not
repeated. An independently committed dispatch fence prevents a duplicate send after
worker cancellation/crash, including a crash after provider acceptance but before the
result is saved. UNKNOWN requires operator investigation; it blocks further automatic
processing for that conversation, with no unsafe retry endpoint.

Human takeover and dispatch serialize on the same channel/conversation locks. After
handover is acknowledged no new automatic dispatch can begin; a message already sent
to the provider cannot be recalled. Sources are checked again for publication and
version before sending. The worker uses a conservative 23h55m reply window measured
from the specific inbound message. No campaigns, proactive messages, templates,
financial tools, media downloads, or browser actions are enabled here.

The model receives the question and selected passages only; source content is treated
as untrusted data. Source IDs are validated, but citations do not prove every statement
is correct. This release cannot make transactional commitments or execute tools.

Delivery callbacks update accepted outgoing IDs. If acceptance was uncertain and no
outgoing ID was saved, delivery cannot yet be reconciled automatically. Staff must check
provider records. Configure database backups, retention/deletion policies and monitoring
before public rollout. Durable dispatch tombstones must outlive provider replay windows.

## Verified interfaces

- Meta message API: https://www.postman.com/meta/whatsapp-business-platform/folder/13382743-ba8d099d-007e-4b52-b9f2-3cf3c60e4fbc
- Meta webhook verification: https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/

## Validation (2026-10-04)

Focused PostgreSQL tests cover the wizard facade, local-only and origin restrictions,
private credential files, validation redaction, activation gates, isolation, deduplication,
manual replies, concurrency, expiry and uncertain outcomes. Browser QA used a separate
local server and fake sender to create an inactive agent and send a human reply, observing
SENT without any Meta traffic. The live local workspace has no connected business number.

## Local real-provider test

The development webhook relay can expose only `/webhooks/whatsapp/{channel_uuid}`
to Meta. Start it with `PYTHONPATH=src .venv/bin/python -m agent_platform.whatsapp_webhook_relay`
(loopback port 4063), then point an operator-managed HTTPS tunnel at that port.
Never point a public tunnel directly at the workspace port 4061. The relay preserves
raw signed bytes, forwards verification parameters, bounds payloads and strips browser
cookies/credentials. All workspace and management routes return 404 on the relay.

A clearly labelled fictional test FAQ may be used to check: opening hours, delivery
and refund policy, an unsupported question, `human` takeover, exact manual reply,
resume for new messages and pause. Confirm both the inbox status and the actual
recipient's WhatsApp message; a SENT row means provider acceptance, not proof of receipt.

## Customer-flow preview

Open `/whatsapp/create` in the loopback console. A customer-style three-step
wizard uses an already provisioned connection, publishes/selects customer FAQs,
saves an inactive agent, and previews answers with the same knowledge engine and
document allowlist used by the WhatsApp worker. Preview never sends a message.
Activation remains blocked until Meta has verified the webhook and the model worker
is available. Existing active agents must be paused before their knowledge changes.

The console is a local customer-experience preview with a server-owned operator
identity. Never expose the console publicly. Its new **Connect a new number with
Meta** action calls the same authenticated service API that a consuming app uses.
Only the separate `/connect/whatsapp/{session}` page and `/webhooks/meta` receiver
are public. Session creation requires an app service credential and its end-user
identity; the server validates asset ownership and encrypts tokens with AES-GCM.

Embedded Signup v4, server code exchange, number registration, WABA subscription,
phone webhook override, periodic health checks and disconnect/reconnect are now
implemented. **Live new-customer onboarding has not been verified.** Meta app
configuration, allowed HTTPS origin, permissions and publication must be completed
once for the platform. Meta requires the shared app webhook to be configured in
its dashboard; customer WABA subscriptions are automatic afterward. Customers
may need their own number verification or billing action in Meta.

See [Meta implementation audit](META_ONBOARDING_IMPLEMENTATION.md) for the current
flow and external prerequisites. A manual test-number connection is not evidence
that Embedded Signup succeeded.

New API endpoints: `PUT /v1/whatsapp/channels/{id}` updates name/documents only
while inactive; `POST /v1/whatsapp/channels/{id}/preview` takes `question` and
`idempotency_key`, enforces channel ownership and generation quotas, and returns
source-backed results plus `handoff` and `sent_to_whatsapp:false`.

## Reviewed multi-recipient sends

`/whatsapp/send` provides sender selection, newline/comma-separated recipient
numbers, Meta-approved text template selection, exact recipient/message review,
explicit consent/confirmation and per-recipient outcomes. The pilot supports up
to 50 unique recipients per batch and 100 preparations per sender per 24 hours.
Only approved text templates without variables/buttons/media are supported.
This is not a free-form outbound campaign builder; AI replies remain in the
inbound service window. Production senders need Meta registration, permissions,
billing and applicable account approvals. Test senders remain subject to Meta's
verified-recipient restriction. Add `waba_id` to the server-side connection record
(the optional operator connection API field) to list the account's templates.

Each prepared recipient/message is immutable. Confirmation atomically queues
selected owned rows; API handlers never send. The worker rechecks the template,
respects recorded STOP/unsubscribe requests, and commits a dispatch fence before
network delivery. Timeouts/crashes become UNKNOWN and are never automatically
retried. Meta acceptance is not delivery: signed webhook receipts separately
record sent/delivered/read/failed. History displays the latest 100 records.
This local console is still not a public customer-authenticated portal.

A production operator must configure webhook verification and subscribe to
messages before relying on delivery receipts, opt-out processing or automatic
replies. Meta may also require app publication before production events arrive.

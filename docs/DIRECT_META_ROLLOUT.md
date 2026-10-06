# Direct Meta rollout

## Current boundary

The reusable app integration defaults to direct Meta. Existing Chakra dispatch
and credentials are preserved. Sahayak's existing embedded Chakra setup UI is
still a legacy integration and must be adapted before new clinics can use this
connector there. No customer number is moved by changing a default or deploying.

### Verified deployment — 6 October 2026

The production API now exposes authenticated WhatsApp and knowledge-document
routes at `https://agents.myalgodesk.in`. The connected-channel endpoint and
non-secret setup diagnostics are deployed. Unauthenticated API requests return
401; the operator console and OpenAPI stay private. The main app health check
passed after deployment.

Meta Embedded Signup configuration `1400861168800001` is configured on the
server. JavaScript login is enabled for `https://agents.myalgodesk.in`.
Twenty local contract tests passed. A live HTTPS check created isolated business
workspaces, verified FAQ isolation, bootstrapped a hosted signup session and
confirmed that an incomplete session cannot return a channel. Temporary test
credentials were revoked and signup sessions cancelled. Worker and model were
online. No customer number was connected and no message was sent by this check.

The shared Meta webhook is now saved and verified. Meta's dashboard enabled
`messages` and `account_update`; the platform's provider readback confirmed both
subscriptions. Meta's synthetic incoming-message webhook test succeeded and the
platform recorded its signed receipt. This verifies webhook transport, not a
real customer's authorization, AI reply or delivery. App category is Business
and pages; `agents.myalgodesk.in` is saved in App domains.

Remaining external gates: the app is unpublished, the business is unverified,
and Tech Provider access verification is blocked on business verification.
The owner supplied corrected business details and confirmed sole-proprietor
status. The verification wizard is in progress; no verification approval has
been obtained. The contact email, privacy-policy URL and data-deletion URL are
saved in Meta. The app icon is prepared but upload needs browser file access.
Its dashboard explicitly
says unpublished apps receive dashboard test webhooks only, not production
messages even for app admins/testers. Configuration `available:true` therefore
does not mean a real customer can receive messages yet.

The small deployment used image
`agent-platform:whatsapp-integration-20261006t030701z`. Its private rollback
configuration is under
`/opt/agent-platform/secrets/whatsapp-integration-20261006T030701Z` on the server.

## Platform operator setup

Configure server-only AI_META_APP_SECRET, AI_META_VERIFY_TOKEN (32+ characters),
AI_META_ENCRYPTION_KEYS and AI_META_ENCRYPTION_KEY_ID. Preserve the encryption
key ring across redeployments. Set AI_META_APP_ID, AI_META_CONFIGURATION_ID,
AI_META_GRAPH_VERSION and AI_META_PUBLIC_URL=https://agents.myalgodesk.in.
Never enter customer access tokens in source control or chat.

In Meta, finish the required business/Tech Provider verification, permission
review and publication for serving other businesses. Complete the Facebook Login
for Business Embedded Signup configuration and allowed HTTPS domains. Configure
the shared callback https://agents.myalgodesk.in/webhooks/meta with the private
verification token, subscribing to messages and account_update. Run both
meta-worker and whatsapp-worker. A successful webhook challenge and provider
subscription readback are required by the existing worker.

Configuration diagnostics do not prove Meta approval. Owner-only verification
and any requested documents must be completed truthfully by the account owner.

## Pilot and migration gates

1. Test direct signup first with a separately authorized test/business number.
2. Verify encrypted tenant-bound credentials, webhook signature, incoming message,
   outgoing delivery, native choices and signed Sahayak booking confirmation.
3. Verify human handover, revocation, expired tokens and duplicate webhook handling.
4. Before moving the existing +91 77039 61610 number, inspect its actual Meta
   ownership and the provider's supported migration/release path. Obtain the
   owner's authorization for any release. Never guess a migration API.
5. Pause the old channel and reconcile outstanding/uncertain sends before changing
   routing. Keep one active sender per number. Do not retry through another
   provider after an uncertain send.
6. Activate direct routing only after the real test passes. Retain a rollback
   record; do not delete Chakra credentials or cancel billing until confirmed.

Applicable Meta charges/rules remain. Removing Chakra removes its intermediary
service dependency, not Meta's control over WhatsApp.

### Public business information — 6 October 2026

Published `/about`, `/privacy`, `/data-deletion`, and `/app-icon.png` on
`agents.myalgodesk.in` only. Pages accurately describe the pilot's manual
deletion process and lack of universal automatic expiry. They contain no
tracking scripts, forms, customer information or credentials. The public
contact is the owner's supplied email; the home street address is not published.
The privacy and deletion URLs and contact email were verified in Meta after
reloading Basic settings. Only the icon remains on that screen's missing-data list.

Deployment image: `agent-platform:public-pages-20261006t034205z`.
Private rollback: `/opt/agent-platform/secrets/public-pages-20261006T034205Z`.
The deployment script is `deploy/update_public_pages.py`; it preserves the
existing image and routes and is a guarded one-time update, not a general release.
Local exact-route and CSP checks passed. HTTPS checks confirm all four public
assets return 200, main-app login remains 200, WhatsApp setup still requires
authentication (401), console/OpenAPI remain 404, and the clinic hostname does
not expose these information pages. The privacy page was visually checked.

Meta verification progressed through proprietor type, identifier, address and
contact details. The owner-authorized CAPTCHA passed. Meta could not locate
a matching business record and now requires an accepted document showing
the entered legal business name, even with domain verification selected.
Accepted types include UDYAM, business bank statement, business registration
or license, and business tax documents. No document was uploaded and no
verification application submitted. Browser policy blocks automated extension
setting changes; the owner must enable file-URL access before icon upload. Identifiers and supporting
documents must remain in Meta, not source control. Public onboarding still
requires completion of business verification, Tech Provider verification,
permission review/publication, and a real authorized end-to-end test.


### Controlled Meta test sender — 2026-10-06

Provisioned an isolated manual Meta pilot connection and FAQ, preserving the
existing clinic/Chakra connection. A seven-day scoped service credential is
stored only in the server secrets directory. The test phone's callback override
is `/webhooks/whatsapp/f25d7d81-4d85-4fcf-9d2c-9ffd1e7a4585` on the public agents
hostname. Meta subscription and callback readback succeeded; webhook verification
was recorded at 09:45:12 UTC. The FAQ preview returned a cited Gemini answer
with no handoff, and activation returned `enabled: true`.

The first incoming signed webhook arrived before the test sender had a route;
no successful message processing or delivery is claimed for that event. A fresh
message from the verified recipient is required to verify inbound processing,
AI response, outbound send and delivery together. Public customer onboarding
remains gated as described above. This manual test setup does not relax the
managed signup's real-number verification checks. Renew expiring test tokens
and the scoped pilot credential before further testing.


Fresh-message verification succeeded on 2026-10-06: inbound at 09:49:37 UTC,
Gemini FAQ reply generated, status SENT, and Meta delivery receipt `delivered`.
This proves the controlled test sender's inbound-to-AI-to-outbound path.
A follow-up at 09:50:06 UTC asking about the business lacked a supporting
business description in the small pilot FAQ. The worker recorded HANDOFF and
set the conversation to HUMAN; no second reply was sent. Further automatic
conversation testing requires completing the business FAQ and explicitly
resuming AI mode. Public onboarding approval remains outstanding.


The owner-approved MyalgoDesk business description was saved as pilot FAQ
version 2 and the test conversation resumed in AUTO mode. The preview request
had an uncertain outcome and was not blindly retried with a new key.
Fresh live questions at 10:22:10 and 10:22:26 UTC on 2026-10-06 both generated
answers citing FAQ version 2, with no missing information. Both reached SENT
and Meta `delivered`; NVIDIA served the first and Gemini the second. AUTO mode
remained enabled. This resolves the earlier missing-business-description test;
public onboarding gates and pilot credential expiry still apply.

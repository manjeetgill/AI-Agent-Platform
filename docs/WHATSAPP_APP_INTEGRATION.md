# Integrating another app

## Boundaries

Provision a parent service credential for the integrating app with `channels:read`,
`channels:write`, `knowledge:read`, `knowledge:write`, `knowledge:publish`. Keep it
on its backend. The app must authenticate its own user and derive
`X-External-User-ID` from that session, never from a browser-supplied owner ID.

Create each customer business with `POST /v1/businesses`:
`{"external_id":"your-stable-business-id","name":"Business name"}`.
Repeated creates return the same workspace. A conflicting owner cannot claim it.
Each business has a separate platform app: even app-shared FAQs stay inside that
business. Parent scopes cannot be used to query the child's documents or inbox.

Issue its backend credential with
`POST /v1/businesses/{id}/credentials`. Choose explicit feature bundles,
for example `{"days":30,"features":["whatsapp_support","whatsapp_notifications"]}`.
This returns the secret once; store it encrypted in your backend and use the
returned `external_user_id`. Issuing a replacement revokes older business keys.
The parent must hold every delegated scope. Never automatically retry a credential
rotation after an uncertain response; reconcile or deliberately rotate again.

Available bundles are `whatsapp_support`, `whatsapp_notifications`,
`whatsapp_calling`, `internet_voice`, `telephone_voice`, and `memory`. The response
returns the exact resulting scopes. Omitted features default to
`whatsapp_support` for compatibility. Request only what that business purchased;
adding a feature requires deliberate credential rotation by the parent backend.
After an uncertain credential response, call
`GET /v1/businesses/{id}/credentials`. It returns the active key ID,
expiry, scopes and feature capabilities without the secret. If a new active key
exists but its one-time secret was lost, rotate deliberately; never replay the
original POST blindly.

The earlier `/v1/whatsapp/businesses` path remains as a compatibility alias. New
integrations should use `/v1/businesses` because the workspace can now subscribe to
WhatsApp, voice, telephony and memory features.

## Default customer flow: direct Meta

New app integrations use Meta directly. Existing Chakra channels keep their
provider and credentials; changing the default does not migrate a number.

1. With the business-scoped credential, call `GET /v1/whatsapp/onboarding/config`.
   `available` means configuration is present, **not** Meta approval. `checks`
   and `missing` contain non-secret setup diagnostics. Public approval remains
   `not_verified` until the operator verifies it outside this API.
2. Call `POST /v1/whatsapp/onboarding` once. Open the returned HTTPS `connect_url`.
   The owner completes Meta authorization/number verification on that page.
   Never put service credentials, access tokens or app secrets into your UI.
3. Persist the session ID in your business's backend record; poll
   `GET /v1/whatsapp/onboarding/{id}` with a bounded timeout. The hosted page
   offers reconciliation for action-required states. Never blindly restart signup.
4. Once CONNECTED, call `GET /v1/whatsapp/onboarding/{id}/channel` to retrieve
   only that owner's healthy channel. An incomplete, expired or revoked
   connection cannot return a channel. This endpoint does not activate replies.
5. Configure the agent: add FAQs and documents for RAG, or explicitly select
   SAHAYAK through the channel workflow endpoint and configure its signed bridge.
   Meta's worker configures/verifies the webhook; do not call Chakra webhook APIs.
6. Preview, activate using the existing readiness gate, and perform a real
   inbound/outbound test. Verify delivery status and an actual booking, then
   expose the connected state to the business owner.

`examples/whatsapp-integration/connect.js` is the default direct-Meta browser
component. Proxy its `/api/whatsapp/onboarding` routes from your authenticated
backend to `/v1/whatsapp/onboarding`; enforce CSRF for writes. Pass a per-business
storage key and clear it at logout. It keeps only the session ID, never the
capability URL. The explicit continuation link avoids popup blocking.

See `DIRECT_META_ROLLOUT.md` for operator prerequisites and migration gates.

## Chakra customer flow (partner pilot)

1. Your backend uses the business credential for all following calls.
2. `GET /v1/whatsapp/chakra/config` checks operator configuration.
3. `POST /v1/whatsapp/chakra/onboarding` returns a short-lived connect token and
   session ID. Pass ONLY this connect token to Chakra's embedded browser SDK.
4. SDK success supplies an untrusted `pluginId`. Send it through your backend to
   `POST /v1/whatsapp/chakra/onboarding/{id}/complete` as `plugin_id`.
5. `GET /v1/whatsapp/chakra/onboarding/{id}` shows saved progress. The browser
   callback alone never authorizes an account binding.
6. Upload business FAQs using `POST /v1/knowledge/documents`, visibility `app`.
7. Once ownership is verified, `GET /v1/whatsapp/setup` lists its available number.
   Create its channel using `POST /v1/whatsapp/channels` with name, phone_id,
   documents. Configure its callback with
   `POST /v1/whatsapp/chakra/channels/{id}/webhook`.
8. Send a real inbound test. Activation stays blocked until its signature verifies
   and the model worker is online. Test the FAQ with the channel `/preview` route,
   then `POST /v1/whatsapp/channels/{id}/activation` with `{"enabled":true}`.
9. Inbox: channel `/conversations` and `/conversations/{id}`. Human takeover uses
   `/conversations/{id}/mode` with `{"mode":"HUMAN"}`. Existing reply endpoint
   remains scoped to the owning business.

`examples/whatsapp-integration/connect-chakra.js` is the legacy customer connection
component. Map its same-origin `/api/whatsapp/onboarding` routes to the Chakra
onboarding API through your authenticated backend. Add CSRF protection to these
backend routes. Use bounded timeouts and show service-unavailable only within the
agent feature. Do not expose the operator console or parent service key.

## Operator setup and remaining provider requirement

Set AI_CHAKRA_PARTNER_TOKEN, AI_CHAKRA_WEBHOOK_SECRET, AI_CHAKRA_GRAPH_VERSION,
AI_PLATFORM_PUBLIC_URL and the existing AI_META_ENCRYPTION_* key ring. The partner
credential is separate from the pilot Bot credential; confirm its Chakra entitlement
and permissions. Keep the matching HMAC secret in Chakra Team Settings.

Chakra documents a clientReferenceId when creating signup tokens, but its current
plugin-config response does not return it. The live pilot read also returned no
customer correlation field. Therefore this release intentionally uses an operator
ownership check for new customers. Independently verify the session's random
reference/plugin name against the customer authorization in Chakra, then run:

```
python -m agent_platform.chakra_onboarding SESSION_UUID --phone-id PHONE_ID --waba-id WABA_ID --ownership-verified
```

The command verifies provider number/account membership and stores encrypted
credentials bound to app, owner and connection. It refuses existing pilot/Meta
numbers and duplicate plugins. It does not treat possession of a browser plugin ID
as ownership. Fully unattended onboarding requires a provider-authenticated
customer-reference lookup or signed signup completion binding. Confirm that
contract with Chakra before replacing the operator check. Do not weaken it.

This code does not purchase/upgrade a Chakra plan, grant partner permissions,
publish an integrating app's login/UI, or prove a second real customer connection.
The integrating backend uses `https://agents.myalgodesk.in` as its base URL.
The proxy exposes authenticated `/v1/whatsapp/*` and `/v1/knowledge/documents`
routes on that host. The operator console stays private. Do not call these
routes directly from a customer browser or expose service credentials there.
An unauthenticated request must return 401; 404 on `/v1/whatsapp/setup` means
the API route or deployed version is missing. An authenticated configuration
response with `available:false` identifies an operator setup issue, not a
problem with the customer's phone number.

Sources: https://chakrahq.com/help/chat/partner/embed-whatsapp-connect-for-your-customers
and https://apidocs.chakrahq.com/api-34338973.

Connection controls: `GET /v1/whatsapp/chakra/connections` returns only this
business's non-secret connection metadata. `POST /v1/whatsapp/chakra/connections/{id}/disconnect`
stops platform sends under the same channel lock used by the dispatcher, cancels
queued replies, and clears the encrypted credential. It does not revoke the
partner's shared API key or remove Meta authorization for unrelated customers.


## Two-app rollout, 2026-10-06

Reach CRM and Sahayak use the same `/v1/whatsapp/chakra` API. The saved
signup's `POST /onboarding/{id}/channel` now accepts `{name, execution_mode,
documents}`. `SAHAYAK` is the existing signed business-application bridge mode
and is used by both apps for compatibility. It does not require a clinic:
Reach maps its organization to the bridge's historical `clinic_id` field.
`RAG` requires published documents belonging to this business. Repeating the
same verified setup returns its channel; changing the definition returns 409.
The no-body clinic contract remains supported.

Reach includes owner/admin authorization controls, server-persisted signup
state (migration 0009), bounded requests and a no-retry state after uncertain
signup. Provider callbacks never prove number ownership. Apply the migration
before restarting Reach. Existing business workflows and consent remain intact.
Each Reach business needs a separate platform app credential; neither customer
service keys nor provider keys are exposed to the embedded SDK.

Server preflight: partner token, webhook secret and graph-version settings were
absent; Sarvam API access was absent too. Reach had no saved platform connection.
Sahayak uses loopback 4065, which its service installer forwards to the server.
No customer number was moved, no subscription purchased, and these changes
have not yet been deployed. Production approval requires real authorization,
verified ownership and a delivery test for each business, not just mocked tests.

### Pilot limits

Migration 029 adds explicitly configured expiring per-business operation caps.
Run `scripts/channel_trial_admin.py APP_UUID --days 7 --whatsapp-turns 25
--whatsapp-sends 25 --voice-calls 5` with the private database environment.
Configure every pilot business separately before activation. This command is an
explicit renewal; it preserves prior usage. There is no automatic renewal or
provider recharge. GET `/v1/whatsapp/usage` returns this app's policy and usage.

Quotas reserve before WhatsApp generation/application work and before all platform
WhatsApp sends (including templates/human replies), and before telephone call
admission. Attempts with uncertain outcomes retain their reservation. Day boundaries
are UTC. Non-pilot apps without a policy retain existing limits; these are NOT
rupee budgets or account-wide provider limits. Previews and web voice have their
existing limits, not this telephone admission quota. Provider rental, subscriptions,
external dashboard use, calls carried before platform rejection and app-side model
usage require provider/account controls. Do not call this a monetary spend cap.

### Expired or uncertain Chakra signup recovery

Migration 067 adds a one-use recovery review and audit history to saved signup
sessions. Status now includes `authorization_expired`, `next_action` and a safe
message. Actions are `resume`, `operator_review`, `renew`, `ownership_review` or
`connected`. Starting another signup for the same business owner while one is
unresolved returns 409 with that saved session ID.

1. Inspect the actual Chakra account or obtain a provider support response. Match
   the plugin to the saved client reference/business; never guess a plugin ID or
   declare that no connection exists while Chakra is unavailable.
2. In the private worker environment run `scripts/recover_chakra_signup.py SESSION
   --reference REVIEW_REFERENCE --verified-plugin PLUGIN_UUID`. Only after
   confirming there is no provider connection may you instead pass
   `--verified-no-connection`. References must not contain credentials or personal
   data. The restricted worker database login is required.
3. The owning app requests `POST /v1/whatsapp/chakra/onboarding/{id}/renew` after
   status reports `renew`. It retains the same app, owner and session ID; an
   existing reviewed plugin is pinned in both the token request and completion.
4. The review is consumed before dispatch. Timeout/crash/invalid response requires
   another provider check after the 55-minute attempt window. No automatic retry
   or duplicate provider creation. A repeated successful renewal returns the
   same saved token while valid. Responses containing tokens use `no-store`.
5. The owner completes Meta authorization; ordinary ownership verification,
   webhook and activation gates still apply. Renewal never proves authorization.

The updated `connect-chakra.js` offers explicit continuation/recovery buttons and
keeps only a per-business session ID in session storage. Proxy GET status and POST
resume/renew/complete through the consuming app backend. Use a unique storage key
and clear it at logout. The example is available for apps to adopt; existing app
frontends are not automatically replaced.

Provider contract checked October 11, 2026:
[Chakra connect-token API](https://apidocs.chakrahq.com/api-28220708).

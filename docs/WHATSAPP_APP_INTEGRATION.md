# Integrating another app

## Boundaries

Provision a parent service credential for the integrating app with `channels:read`,
`channels:write`, `knowledge:read`, `knowledge:write`, `knowledge:publish`. Keep it
on its backend. The app must authenticate its own user and derive
`X-External-User-ID` from that session, never from a browser-supplied owner ID.

Create each customer business with `POST /v1/whatsapp/businesses`:
`{"external_id":"your-stable-business-id","name":"Business name"}`.
Repeated creates return the same workspace. A conflicting owner cannot claim it.
Each business has a separate platform app: even app-shared FAQs stay inside that
business. Parent scopes cannot be used to query the child's documents or inbox.

Issue its backend credential with
`POST /v1/whatsapp/businesses/{id}/credentials` and `{"days":30}`.
This returns the secret once; store it encrypted in your backend and use the
returned `external_user_id`. Issuing a replacement revokes older business keys.
The parent must hold every delegated scope. Never automatically retry a credential
rotation after an uncertain response; reconcile or deliberately rotate again.

## Customer flow

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

`examples/whatsapp-integration/connect.js` is an embeddable customer connection
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
Private API ingress must be arranged for the integrating backend; the current
pilot's public proxy exposes only webhooks and connection pages.

Sources: https://chakrahq.com/help/chat/partner/embed-whatsapp-connect-for-your-customers
and https://apidocs.chakrahq.com/api-34338973.

Connection controls: `GET /v1/whatsapp/chakra/connections` returns only this
business's non-secret connection metadata. `POST /v1/whatsapp/chakra/connections/{id}/disconnect`
stops platform sends under the same channel lock used by the dispatcher, cancels
queued replies, and clears the encrypted credential. It does not revoke the
partner's shared API key or remove Meta authorization for unrelated customers.

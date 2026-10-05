# Meta onboarding implementation audit

## Current flow (before this revision)

User → local operator console → manual token/secret/number entry → private JSON
file → inactive channel → manual public webhook verification → activation.
Customer wizard reuses that saved connection. Multi-send has durable approvals
and an independent dispatch fence. Neither wizard performs Embedded Signup.

| Missing | Current behavior | Expected behavior | Implementation |
| --- | --- | --- | --- |
| Embedded Signup | Manual assisted connection | Meta v4 popup and server callback | Short-lived owner-bound onboarding session and hosted connect page |
| Customer credentials | Global operator JSON file | Encrypted per-owner credentials | AES-GCM records, server-only rotating key ring |
| Asset association | User supplies number ID | Verify returned WABA and phone with Meta | Token introspection, WABA phone enumeration, exclusive ownership |
| Webhook onboarding | Manual per-channel callback | Automatic verified WABA subscription | Signed shared receiver, one-time platform callback, customer subscription/health worker |
| Durable state | No onboarding state | Reload/retry survives process loss | PostgreSQL state, claim lock, external-action reconciliation |
| Lifecycle | File credentials remain valid indefinitely | Recheck, reconnect, disconnect | Stored expiry, health checks, dispatch-time validity gates |
| Public user identity | Loopback operator only | New app users remain isolated | App-authenticated session creation; narrowly scoped browser capability |

## Official requirements checked 2026-10-04

Meta implementation documentation updated July 24, 2026 specifies Embedded Signup
v4, HTTPS allowed domains, Facebook Login for Business configuration and SDK,
and a 30-second exchangeable code lifetime. New connector uses configurable
Graph v26.0. Existing manual connections retain their selected version.

Sources:
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider

Platform-level app permissions, Tech Provider eligibility, approved HTTPS domain,
Meta configuration ID and publication are prerequisites controlled by Meta.
They must be distinguished from customer onboarding. A customer may still need
number verification, an existing number PIN or billing action. Never report
CONNECTED solely from the browser FINISH event or a successful subscription POST.


App-level WhatsApp webhook subscriptions cannot be created through POST
`/{app-id}/subscriptions` (Meta Graph v26 documentation explicitly excludes
WhatsApp). The platform operator sets the shared callback and message/account
fields once in the Meta app dashboard. The backend checks GET subscriptions and
an actual successful verification challenge. Customer WABA subscription and
phone callback override are then automatic; customers never enter webhook values.
Source: https://developers.facebook.com/docs/graph-api/reference/v26.0/app/subscriptions


## Deployment and app integration

Apply migrations through `018_meta_onboarding.sql`. Install dependencies, including
`cryptography`, and run `python -m agent_platform.meta_worker` alongside the API
and WhatsApp worker (`compose --profile whatsapp` includes it). The local console
starts the worker itself. Keep app secrets and `AI_META_ENCRYPTION_KEYS` in the
server secret store; preserve old key IDs when rotating so existing records can
be decrypted. Back up keys separately from encrypted database records.

Configure the `AI_META_*` variables documented in `.env.example`. The app ID and
Facebook Login configuration ID are public identifiers; app secret, verification
secret and encryption key ring are server-only. The one-time shared callback is
`AI_META_PUBLIC_URL/webhooks/meta`. Configure `messages` and `account_update` in
the Meta app dashboard. The backend requires both the actual verification
challenge and subscription readback before it reports a connected customer.

An integrating app's backend calls `POST /v1/whatsapp/onboarding` using its scoped
service credential and authenticated end-user ID. It opens the returned
`connect_url` for that user, then polls `GET /v1/whatsapp/onboarding/{id}`. Never
put the service credential into a browser. The hosted link carries a 30-minute
capability in its fragment, exchanges it for a Secure/HttpOnly cookie, and removes
the fragment before loading Meta's SDK. Callback writes also require the exact
HTTPS Origin. Meta's short-lived code is sent directly to the backend and is
never placed in local/session storage. The database stores only its hash.

After connection, the app lists the user's numbers with `GET
/v1/whatsapp/connections`, selects knowledge, previews an answer, then activates
its channel. Disconnect disables messaging immediately and reconciles remote
subscription state. Tokens are rechecked periodically and at dispatch through
fresh health/expiry requirements. Reauthorization uses a new Meta session;
there is no invented refresh-token mechanism for Meta business tokens.

## Verification record (2026-10-04)

19 focused WhatsApp/onboarding/relay tests passed against disposable PostgreSQL.
Tests cover encrypted owner binding and key rotation, wrong app credentials,
callback origin and owner checks, uncertain registration without duplicate POST,
challenge plus subscription requirements, revocation, reconnect deduplication,
foreign WABA ownership, signed webhook routing/deduplication and restricted relay
headers/routes. Provider responses in these tests are simulated explicitly.
The local customer UI loads correctly and keeps the Connect action unavailable
while Meta configuration is missing. No real new customer has completed signup
and no real outgoing message has been sent as part of this revision.

The current Meta app is unpublished. Its dashboard says production webhooks
require publication. Creating its 60-day Embedded Signup configuration is also
awaiting explicit approval of the access scope. These are live-test blockers,
not customer tasks that should be repeated for every new account.

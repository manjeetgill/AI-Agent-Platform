# WhatsApp notifications for consuming apps

This contract lets a consuming app deliver its own event notifications through an
owned WhatsApp Business sender. The app owns the number and provider authorization;
the platform owns recipient verification, consent, formatting, quotas, durable
dispatch and delivery state. The service never performs a payment or transaction.

## Backend setup

Provision one platform app boundary per business workspace with `channels:read`,
`channels:write`, `knowledge:read`, `knowledge:write`, `knowledge:publish` and
`alerts:send`. Keep the service credential on the app backend. Send a stable
`X-External-User-ID` derived from the authenticated business owner.
The workspace credential API supplies these permissions when the parent requests
`whatsapp_support` plus `whatsapp_notifications`.

1. Connect the business number through Meta/Chakra onboarding and create, verify and
   activate its WhatsApp channel.
2. Receive one signed webhook containing the provider's trusted display number.
   Meta onboarding also supplies this value when available.
3. `PUT /v1/whatsapp/alerts/route` with the owned `channel_id` and daily limit. The
   platform refuses channels belonging to another app/owner. Operator-approved shared
   senders remain a separate administrative route.
4. For each signed-in recipient, create a verification and show the returned exact
   `VERIFY …` phrase once. The user sends it from the claimed number to the displayed
   sender. Poll the verification resource, then enable the subscription.
5. Create a notification with a stable business event ID as `request_key`. Poll the
   delivery resource after an uncertain client response; never invent a new key and
   resend blindly.

The generic notification body is `{request_key, category, title, summary, reference?,
occurred_at, test_mode}`. The platform adds the app name, timestamp, safety footer and
recipient from the verified subscription. Apps cannot override the recipient in a
delivery request. Market alerts keep their more specific `/deliveries` schema.

Free-form notifications require a recent inbound WhatsApp conversation. Approved
template delivery outside that window is not implemented. Provider acceptance is not
proof of delivery; wait for the signed `delivery=delivered` or `read` receipt. Disable
the route or subscription to stop new messages. Provider charges and the app's own
privacy/retention obligations still apply.

Python and TypeScript backend clients are in `examples/whatsapp-notifications/`.
They deliberately omit automatic mutation retries and browser-side credentials.

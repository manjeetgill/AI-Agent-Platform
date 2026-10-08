# App-owned WhatsApp alert delivery

An app's user supplies a **personal WhatsApp recipient number** and opts in. The
user does not create a Meta business account. The platform operator binds that
app to a connected WhatsApp Business sender in `whatsapp_alert_routes`. This
binding is separate from the sender's inbound conversation agent; it does not
change Sahayak booking replies.

The app backend holds a scoped `alerts:send` service key. With its stable
`X-External-User-ID`, it calls:

1. `POST /v1/whatsapp/alerts/verifications` with `recipient`. The response
   returns a one-time `VERIFY …` message. Show it once and ask the user to send
   it from that number to `sender_number`. The platform stores only its hash.
2. Poll `GET /v1/whatsapp/alerts/verifications/{id}` until its state is
   `VERIFIED`, then call `PUT /v1/whatsapp/alerts/subscription` with `recipient`
   and `consented:true`. The proof is single-use, expires after ten minutes and
   is scoped to the app and external user. An unchanged active subscription is
   idempotent. `DELETE` disables the subscription and cancels queued sends.
3. `POST /v1/whatsapp/alerts/deliveries` with a stable `request_key`, exchange,
   symbol, timeframe, condition, target, observed price, and trigger time.
4. `GET /v1/whatsapp/alerts/deliveries/{id}` for dispatch and provider receipt
   states. `ACCEPTED` means provider acceptance; `delivery=delivered` or `read`
   comes from a signed WhatsApp webhook.

The platform formats the alert, enforces the operator's per-app daily cap and
trial quota, stores the exact message before the worker sends it, and never
blindly retries an uncertain provider outcome. The consuming app keeps its own
alert rule and event history; the platform stores delivery metadata. The
operator can restrict a pilot route to one external user ID.

This pilot sends free-form text only within WhatsApp's recent-message window.
For alerts outside that window, an approved alert template and its applicable
pricing are required. A closed window is a visible failure, never a silent or
non-compliant send. A consuming app can continue in-app alerts when this service
is unavailable.

Apps with their own connected sender can configure the route through
`PUT /v1/whatsapp/alerts/route`; see [generic app notifications](WHATSAPP_APP_NOTIFICATIONS.md).
Operator-approved shared senders continue to use a restricted administrative binding.

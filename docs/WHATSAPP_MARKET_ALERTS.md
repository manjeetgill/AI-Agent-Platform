# App-owned WhatsApp alert delivery

An app's user supplies a **personal WhatsApp recipient number** and opts in. The
user does not create a Meta business account. The platform operator binds that
app to a connected WhatsApp Business sender in `whatsapp_alert_routes`. This
binding is separate from the sender's inbound conversation agent; it does not
change Sahayak booking replies.

The app backend holds a scoped `alerts:send` service key. With its stable
`X-External-User-ID`, it calls:

1. `PUT /v1/whatsapp/alerts/subscription` with `recipient` and `consented:true`.
   The recipient must have messaged the bound sender recently and must not have
   opted out. This proves a session is open, not that the app user owns the
   recipient number. Public multi-user rollout also needs a code or signed
   link returned in that WhatsApp chat. `DELETE` disables the subscription and
   cancels queued sends.
2. `POST /v1/whatsapp/alerts/deliveries` with a stable `request_key`, exchange,
   symbol, timeframe, condition, target, observed price, and trigger time.
3. `GET /v1/whatsapp/alerts/deliveries/{id}` for dispatch and provider receipt
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

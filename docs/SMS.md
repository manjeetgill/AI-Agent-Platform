# SMS notifications (SMSIndiaHub, Traccar pilot and mock)

The same platform and scoped application credentials used by email/WhatsApp can now submit transactional SMS. Separate app routes, encrypted transient payloads, a durable worker queue and forced row-level security preserve tenant/app/user isolation. No new subscription or SDK is required to install the adapter. SMSIndiaHub account access and credits are separate.

## What is implemented

- `POST /v1/notifications/sms` queues a single recipient with an idempotency UUID; `notifications:send` scope required.
- `GET /v1/notifications/sms`, `/{id}`, `/templates`, `/usage`, `/configuration` require `notifications:read`. Send/list/detail expose no phone number, message, OTP or private credential.
- Worker leases, safe connection/rate-limit retries with backoff (maximum five attempts), expiry and no blind retry after ambiguous transmission or a worker crash.
- Explicit `mock` and `smsindiahub` providers; no automatic fallback. Mock returns `MOCK_SENT`, live acceptance returns `ACCEPTED`. Neither means handset delivery.
- API key is sent only in the HTTPS POST body to the fixed provider endpoint. Redirects are disabled. Raw provider errors are never logged.
- Request and attempt caps per app and globally, by UTC day/month/rolling minute. Caps reserve **SMS segments**, including retries; Hindi/Unicode and GSM extension characters are counted. Default one segment per message, 10/day, 100/month, 5/minute; zero daily/monthly disables that scope. Counts are conservative reservations, not billing totals or a rupee budget.
- Transactional templates only. Marketing broadcasts are unsupported. Consuming apps remain responsible for the requested/consented purpose and selecting the right recipient; the platform does not generate or validate login OTPs.
- Workspace: `/monitor/sms`, linked from Agent Workspace and its setup checklist. This screen is read-only and uses the selected app connection. Missing scopes/configuration are shown; secrets are never pasted in the dashboard.

## Local setup

Apply migration 055 with the migrator role using the normal migration command. API and worker must use their separate restricted roles. Restart both after configuration. Do not edit old migrations.

Register a consuming app and give its backend `notifications:send` and `notifications:read` scopes. Then, from the repository using its Python environment:

```sh
python scripts/configure_sms.py --app-id APP_UUID --provider mock --templates examples/sms-templates.mock.json
```

This saves one app's route to private `.runtime/local/config.json`, preserves other apps and existing notification encryption keys, and never sends a message. Add the app's private client file to the existing local monitor connections file to see its activity. The script expects the local operator's existing restricted database role file.

For live sending, obtain an SMSIndiaHub account API key and approved six-letter sender header, principal entity (PE) ID and content template IDs. Copy the template file privately; replace the sample text with each **exact approved template**, representing its allowed variable positions as `{code}`, `{doctor}`, etc. Add `"template_id":"YOUR_DLT_TEMPLATE_ID","approved":true` per template. Operator attestation does not independently prove DLT approval; confirm it in the provider account.

```sh
python scripts/configure_sms.py --app-id APP_UUID --provider smsindiahub \
  --templates /absolute/private/approved-sms-templates.json \
  --sender-id ABCDEF --pe-id YOUR_PE_ID --daily 10 --monthly 100 --per-minute 5
```

The API key is entered at a hidden terminal prompt. Never put it in a command argument, frontend bundle, Git or chat. Existing queued messages keep their route fingerprint; a changed sender/provider/template pauses them for configuration review until expiry, rather than silently redirecting them.

For hosted installations use server-side environment/secret-manager values:

- `AI_SMS_NOTIFICATIONS=1` in API and worker.
- `AI_SMS_APPS`: JSON keyed by platform app UUID; same Config shape as the script generates.
- `AI_SMS_GLOBAL_LIMITS`: platform-wide segment caps.
- Per-app `key_env`: name of the private environment value containing that account's key.
- Existing `AI_NOTIFICATION_ENCRYPTION_KEY_ID`, `AI_NOTIFICATION_ENCRYPTION_KEYS` (base64 AES-256 keyring), and `AI_NOTIFICATION_HASH_KEY` (base64 32-byte HMAC key). Preserve these across restarts; do not rotate them by overwriting queued-data keys.

## Consumer backend request

The frontend calls its own backend. Only the backend possesses the scoped platform key and derives `X-External-User-ID` from the signed-in user.

```http
POST /v1/notifications/sms
Authorization: Bearer <private application service key>
X-External-User-ID: <authenticated app user>
Content-Type: application/json

{
  "idempotency_key": "d2db1ea3-5904-494e-b79f-aea7b8cf2dd3",
  "to": "+919876543210",
  "template": "otp",
  "variables": {"code": "123456"},
  "expires_in_seconds": 300
}
```

Number above is illustrative; use only an authorised test recipient. This first adapter accepts Indian mobile recipients with country code 91, one per request. For multiple recipients enqueue independent requests with distinct idempotency keys within limits. Reuse the same key for retries of the same request. Poll the returned ID. Mock tests cost no provider credits; live tests require configuration and recipient approval.

## Delivery and pricing limitations

The provider's [official API page](https://www.smsindiahub.in/free-sms-gateway-developer-api/) documents the POST payload and acceptance response. It advertises test credits, not unlimited free production SMS. Verify account eligibility, current price per segment, taxes, DLT requirements and credits with the provider before enabling live sends; there is no paid default.

The public material does not establish an authenticated callback contract or provider-side idempotency for this endpoint. Automated delivery receipts are therefore **not implemented**; no unauthenticated delivery webhook is exposed. `DeliveryDate` in a submission response is not treated as delivery confirmation. Keep provider JobId and MessageId for a manual account check. For `OUTCOME_UNKNOWN`, inspect the provider state before considering a new request. Add authenticated DLR support only after obtaining the account's documented payload and authentication mechanism.

## Verification

Run SMS, existing email and security tests with `scripts/test_restricted.py` against a disposable PostgreSQL database. The suite exercises mocked HTTP contracts, real queue processing, encryption, quota reservations, cross-app/owner isolation, retries, crash recovery and anonymous-route rejection. No live SMS is sent by these tests.

## Traccar Android SIM pilot (local network)

`traccar_local` replaces the retired SMSGate adapter. It uses Traccar SMS Gateway's
local HTTP API on your phone, with the existing scoped SMS API, encrypted outbox,
worker, idempotency and segment quotas. No Traccar public relay is used.
This is a small test-recipient pilot, not a multi-customer telecom service.
Carrier charges and permitted-use terms still apply.

### Phone setup

1. Install Traccar SMS Gateway from Google Play:
   https://play.google.com/store/apps/details?id=org.traccar.gateway
2. Review requested SMS permissions and any default SMS app prompt on the phone.
3. Open Gateway settings, enable the local gateway, and keep the phone powered
   on the same trusted Wi-Fi as the platform worker.
4. Note the local URL (normally port 8082) and keep its **local API key** private.
   The cloud token is not used. Do not forward the phone port to the public internet.
5. Confirm the intended sender SIM slot. Platform setup uses 1, 2 or 3; the adapter
   converts this to Traccar's zero-based slot. Caller ID comes from that SIM.

### Private operator setup

Replace APP_UUID and the example address with the existing app and real phone.
The planned test sender is 7703961610; recipient is 9871928155. Confirm the SIM
slot on the device before sending. Setup does not validate which number owns a SIM.

```sh
python scripts/configure_sms.py --app-id APP_UUID --provider traccar_local \
  --gateway-url http://192.168.1.50:8082 --allow-lan-http --sim-slot 1 \
  --test-recipient +919871928155 --templates examples/sms-templates.android-pilot.json \
  --daily 5 --monthly 20 --per-minute 1
```

Enter the local API key only in the hidden terminal prompt. It is stored privately
under an environment-variable reference, never returned to frontend clients.
Only this app's route is changed. Apply migration 057 and restart API/worker.

```sh
python scripts/configure_sms.py --app-id APP_UUID --check-only
```

The check POSTs an empty JSON object. Traccar authenticates it, then returns HTTP
500 with `Missing phone or message` without invoking its SMS handler. This exact
validation response is expected only for this check. GET is public and cannot
verify the key. Neither check nor setup sends an SMS or reads the phone inbox.
It does not certify balance, SMS permissions, SIM identity or actual delivery.

### Queue and delivery semantics

- Same `/v1/notifications/sms` API and per-app permissions as other providers.
- Up to ten explicit test recipients, one segment per message, templates and quotas.
- Local API uses raw `Authorization: <local key>` and POST `/` JSON `to`, `message`,
  `slot`. No Basic auth, cloud token or unsupported fields.
- Traccar HTTP 200 with an empty body means the Android send API was invoked.
  Platform status is ACCEPTED; provider message ID is null. This is not delivery.
- No device deduplication, status lookup, expiry or automatic delivery receipt API
  is supported by this connector. The platform applies expiry before dispatch,
  but cannot cancel or expire a message already handed to the phone.
- Queue idempotency prevents duplicate app requests. Only connection failures
  before dispatch retry automatically. Read/write timeout, worker crash, unexpected
  response or server error becomes OUTCOME_UNKNOWN. Never blindly resend;
  inspect the sending phone and receiving handset before creating another request.
- No inbound SMS ingestion or voice/WhatsApp calling is added.
- Local HTTP exposes the key/content to the LAN. Explicit opt-in is required.
  Only fixed RFC1918 IPv4 URLs with a port are accepted; no DNS/public/metadata
  hosts, redirects, URL credentials, query strings or path overrides. HTTPS
  certificate verification stays enabled. A remote server cannot reach a home
  phone without a separately configured private network.
- Review device message retention; platform logs/dashboard omit message content.
- Migration 056 is immutable. `android_local` is retained only as a historical DB
  provider value; runtime configuration rejects it and its adapter has been removed.
  Previously queued retired-route jobs require configuration review, never rerouting.

Protocol checked against official source revision
`216a8ae363ca00ba03be2ecd1ed33a28c30f4c4d` on 2026-10-10:
https://www.traccar.org/http-sms-api/
https://github.com/traccar/traccar-sms-gateway/blob/216a8ae363ca00ba03be2ecd1ed33a28c30f4c4d/app/src/traccar/kotlin/org/traccar/gateway/GatewayServer.kt
https://github.com/traccar/traccar-sms-gateway/blob/216a8ae363ca00ba03be2ecd1ed33a28c30f4c4d/app/src/traccar/kotlin/org/traccar/gateway/GatewayServiceUtil.kt

### Traccar pilot testing and diagnostics

`--testing-no-volume-caps` disables application and platform daily/monthly caps for only the configured Traccar pilot. Per-minute pacing, allowed recipients, single-segment messages and idempotency remain enforced. Other applications retain their own limits. Remove the flag to restore volume caps. Usage is still recorded and carrier charges still apply. Configuration/usage APIs return `null` for disabled caps.

The no-send connection check probes authentication and parsing of the configured zero-based SIM slot, without sending a recipient or message. It cannot verify SIM availability, balance or delivery. API sends classify permission, SIM/subscription, JSON parsing and other server failures into fixed diagnostic codes. Raw responses are discarded; earlier generic failures cannot be diagnosed retroactively. Uncertain sends are never automatically retried.

## Phone login integration (consuming application)

Entering a phone number does not authenticate a user. The app owns the login challenge and session; this platform transports its OTP. No LLM is involved in generating or verifying login codes.

1. The app backend normalizes the requested number and applies per-number/IP resend and failed-verification limits. Keep these login controls even when the platform pilot volume cap is disabled. Do not reveal whether an account exists.
2. Generate a cryptographically random OTP. Store a keyed hash of it with a challenge ID, recipient binding, expiry and attempt count in the app database. Keep the plaintext code only transiently for the SMS request. Make successful verification atomic and single-use.
3. The app backend calls `POST /v1/notifications/sms` with its private app credential, template `otp`, `{"code":"<generated code>"}`, and an expiry no later than the challenge's remaining lifetime. The pilot template is in `examples/sms-templates.android-pilot.json`.
4. Before login there is no authenticated user session. Use an app-generated opaque pending-login identity for `X-External-User-ID`, bound server-side to the challenge. Do not trust an arbitrary identity supplied by the browser. Reuse the same identity and idempotency key when reconciling that request.
5. Persist the platform job ID with the challenge. `QUEUED` means waiting; `ACCEPTED` means the phone API accepted it, not delivered. `OUTCOME_UNKNOWN` must not cause an automatic resend. An explicit resend follows the app's cooldown and invalidates/replaces the old challenge as appropriate.
6. Verify the submitted code in the app backend, checking binding, expiry and attempt count. Create the login session only after successful verification. Never grant login based on SMS delivery status.

The browser calls only its own app backend; provider and platform keys stay server-side. Each consuming app needs its own platform app identity, `notifications:send/read` scopes, SMS route and templates. Configuring the sandbox does not automatically connect another app.

**Current local pilot:** Traccar sends only to operator-allowlisted test recipients, and the phone must remain reachable from the worker on its local network. A random new user's number is rejected until explicitly enabled for the pilot. This is not yet an unrestricted public login SMS service. Production delivery requires an appropriate configured route and its applicable sender/template authorizations.

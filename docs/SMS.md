# SMS notifications (SMSIndiaHub + mock)

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

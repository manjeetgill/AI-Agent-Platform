# Self-managed speech, email and WhatsApp transport

The operator workspace has **Self-hosting** at `/self-hosting`. It reports the
current local process configuration, not billing status or production readiness.
Apps continue to use the same authenticated voice and email APIs with their own
identities, tools, limits and data. No provider subscription is created by setup.

## Customer service options

In **Connected apps → Choose how we provide your services**, the operator can
save an app's preference. Both options are operated and offered by this platform:

| Capability | Use our service | Use a provider-backed service |
| --- | --- | --- |
| Speech | Local speech engines on platform servers | Sarvam through the platform |
| Email | Platform-operated SMTP | Resend or Brevo through the platform |
| WhatsApp | Platform's direct Meta connector | Chakra through the platform |

Customers do not supply provider credentials or need separate provider accounts
for these offerings. Business owners still authorize their WhatsApp numbers.
The platform operator pays its vendors and sets the customer-facing prices and
limits. The UI does not invent a free plan or an extra rupee amount: pricing is
`QUOTE_REQUIRED` until an offer is established. Meta charges may apply to either
WhatsApp option. Core answer-model costs are separate from speech transport.

Consuming backends can read `GET /v1/integrations/service-options` with
`services:read` and request a choice using `POST` on the same path with
`services:write`. Use the existing service authentication and external-user
headers. Example body:

```json
{"capability":"email","route":"platform","provider":"smtp","expected_revision":0}
```

Use the returned revision on later changes; stale changes return 409. Choices
belong to the authenticated app, are isolated from other apps and have a durable
audit trail. The local operator can save preferences without widening an app's
existing key permissions. Grant the new scopes explicitly only to an app backend
whose administrators may manage service preferences.

**This is the service selection/setup-request stage, not automated commercial
activation.** Saving returns `SETUP_REQUESTED` and `activated: false`. It does not
change live routes, subscribe to a provider or charge anyone. The operator still
needs to establish pricing, provision the chosen route and verify live delivery
or speech before activating it through the existing channel configuration.
Automatic billing and a verified activation workflow are not implemented here.

## Offline speech (no Sarvam account)

Install [whisper.cpp](https://github.com/ggml-org/whisper.cpp) and
[eSpeak NG](https://github.com/espeak-ng/espeak-ng) on the platform worker host.
Use a multilingual Whisper GGML model (`base` or `small`, not an `.en` model) for
Hindi/English tests. Download models during operator setup, never in a call.
Review the licenses of binaries and model weights when distributing them.

Server environment (or entries in the private local `config.json`):

```text
AI_CALLING_SPEECH_PROVIDER=local
AI_LOCAL_STT_BINARY=/absolute/path/to/whisper-cli
AI_LOCAL_STT_MODEL=/absolute/path/to/ggml-base.bin
AI_LOCAL_TTS_BINARY=/absolute/path/to/espeak-ng
AI_LOCAL_STT_LANGUAGE=auto
```

Restart the voice/console workers after configuration. Test English and Hindi
names, dates and Hinglish before enabling this route for customers. eSpeak is
functional but less natural than neural speech services; model accuracy and CPU
latency depend on your hardware. No cloud speech fallback is used in `local` mode.
This replaces recognition and synthesis, **not the separately configured answer
LLM**, which may still use Gemini/NVIDIA. Browser calls do not require a carrier;
ordinary mobile-number calls still require an authorized carrier connection.

Whisper receives at most 12 seconds of mono 16-bit 16kHz PCM and runs with two
threads and a 30-second timeout. Runtime call duration and concurrency bounds
continue to apply. Each call turn uses a separate mode-0700 temporary directory;
input WAV and transcription JSON are deleted on completion, failure or cancellation.
For RAM-only temporary audio, set `AI_LOCAL_SPEECH_TMPDIR` to a private tmpfs
directory on Linux. A host crash can leave temporary files: clear expired
`agent-speech-*` directories at boot, before starting workers. Logs do not contain
raw transcripts or audio. The existing call records may retain text conversation.

## Email through your SMTP server (no Resend/Brevo account)

Keep `AI_EMAIL_NOTIFICATIONS=1` and the existing notification encryption/hash
settings. Apply migration `048_email_smtp.sql` through the standard migration
runner. Add or update only the intended app entry in `AI_EMAIL_APPS`:

```json
{
  "YOUR-APP-UUID": {
    "provider": "smtp",
    "from_email": "notifications@your-domain.example",
    "from_name": "Your app",
    "key_env": "YOUR_APP_SMTP_PASSWORD",
    "smtp": {
      "host": "mail.your-domain.example",
      "port": 587,
      "security": "starttls",
      "username": "notifications@your-domain.example"
    },
    "allowed_link_hosts": ["your-domain.example"],
    "limits": {"daily": 100, "monthly": 1000, "per_minute": 10}
  }
}
```

Store the password in the server environment named by `key_env`; never send it to
an app/browser or commit it. `tls` supports implicit TLS, usually port 465.
Certificates are verified. TLS failure does not downgrade to plaintext.
For a **same-host relay or development sink**, use literal `127.0.0.1` or `::1`,
`security: loopback`, the relay port, and omit username/key_env. Plaintext remote
SMTP and authenticated plaintext SMTP are rejected. SMTP host selection is
operator-only, not a user-supplied URL or a tool argument.

Use the existing `/v1/notifications/email` endpoint with `notifications:send` and
an idempotency key. Templates, encryption, per-app/global limits, worker leases,
suppression checks and audit records apply unchanged. The queue pins the route;
changing a route does not silently move pending email to a different provider.

`ACCEPTED` means **your SMTP relay accepted the message**, not that it reached an
inbox. SMTP does not currently have delivery callbacks in this platform; delivery
remains `UNCONFIRMED`. Monitor your relay's queues/bounces separately. A negative
temporary SMTP response can be retried; a disconnect after DATA or worker crash
is `OUTCOME_UNKNOWN` and is not blindly replayed. Message-ID is correlation only,
not a server deduplication guarantee. Raw SMTP replies are not logged.

An SMTP adapter is not a full mail server. Operate an MTA such as Postfix yourself
or connect an existing authorized relay. Before internet delivery configure DNS
SPF, DKIM signing in the MTA, DMARC, reverse DNS and a bounce mailbox; confirm
outbound SMTP is permitted by your host. Protect against open relay. Hosting,
IP reputation and deliverability remain your responsibility. Local mock or sink
tests do not prove Gmail/Outlook delivery and incur no external delivery charges.

## WhatsApp directly through Meta (no Chakra account)

The direct Meta connector already exists. In **WhatsApp → Connect your business
number**, save the authorized phone-number ID, Graph API version, access token,
app secret and webhook verify token. For other consuming apps, use their scoped
onboarding/connection APIs and app identity; do not share the console identity.
Complete Meta authorization and HTTPS webhook verification, subscribe the app to
the WhatsApp business account, then verify inbound routing and an approved reply.
Enable the agent only after that test. Existing Chakra channels are not converted
automatically; migrating a number requires its owner's authorized onboarding.

WhatsApp still runs on Meta's infrastructure. Business/permission approval,
number eligibility, calling eligibility and applicable Meta charges remain.
The platform cannot self-host WhatsApp or bypass its authorization requirements.

## Validation and remaining setup

Automated tests cover offline dispatch, cancellation cleanup, SMTP TLS refusal,
explicit failure vs ambiguous acceptance, a loopback SMTP sink, the real email
queue, existing provider regressions and local-only redacted setup status.
These tests do not evaluate real speech accuracy or public email deliverability.
No existing app route, provider account, DNS or WhatsApp number is changed by
adding this capability. Install the speech assets and supply your own working
SMTP relay before selecting those routes for real users.

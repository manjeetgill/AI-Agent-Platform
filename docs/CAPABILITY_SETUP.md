# Capability setup and verification

Open <http://127.0.0.1:4061/setup>. The checklist reads saved state without calling
providers, spending model quota, activating channels, sending messages or placing calls.
Select the local operator workspace or an existing application's private connection.
Setup links open local operator screens; they do **not** change the selected app's identity.
External apps continue to use their scoped service credentials and API.

`GET /v1/integrations/capability-readiness` returns eleven capability entries with
status, next steps and redacted evidence. The API checks permissions independently
for each capability. One unavailable service does not hide the others. Counts cover
the first page and configured state does not certify a live integration.

| Capability | Platform verification | External completion |
| --- | --- | --- |
| Planning | Task-readiness checks scopes, tools, quota, migrations and workers; synthetic plan can run via the real worker | Working model account and allowance |
| WhatsApp | Existing setup, sender check, activation gates, webhook signature and worker tests | Authorised sender, public HTTPS callback, provider verification and real receipt/reply |
| WhatsApp calling | Connector, eligibility check, activation and audio worker tests | Calling-eligible number, owner authorization, real call |
| Internet voice | Speech provider shown accurately; text preview and local WebRTC tests | Human microphone, speaker and app action test; optional TURN for restrictive networks |
| Telephone | Asterisk gateway, app isolation, carrier verification and routing tests | App-owned carrier, number authorization and inbound call |
| Email | Mock queue, encryption, provider adapters and bounded delivery tests | Verified sender, provider key and real receipt |
| Email tracking | Signed callbacks, duplicate handling and suppression tests | Register callback with provider and observe real event |
| Knowledge | Upload/update/delete documents, retrieval and cited answer | Each app's approved documents and factual review |
| Feeds | Approved profile, secret storage, bounded connection tests | Authorised source credentials and usable observations |
| Schedules | Bounded schedule, approval, source cooldown and worker tests | Working source adapter, selected frequency and limits |
| Browser runner | Pairing, scoped site access and user takeover | User pairs extension; optional, not required for server/API tools |

## Knowledge

The Memory & knowledge screen now accepts text documents and FAQs, private by
default. App sharing must be selected explicitly. The same external ID updates a
document, changes its version and invalidates old citations. Test source retrieval
without model usage, then verify the generated answer and its citations.
Only the owner may delete a document, even when another user can read it.

## Live email configuration

For an existing app with an email route, use:

```sh
.venv/bin/python scripts/configure_transactional_email.py \
  --app-id APP_UUID --provider resend --sender notifications@example.com \
  --link-host example.com --public-url https://agents.example.com \
  --daily 10 --monthly 100 --per-minute 5
```

Use `--provider brevo` for Brevo. The script asks for secrets with hidden input,
preserves other applications and the encryption keyring, and writes the private
local configuration. It never sends email or claims domain verification. Restart
the API and worker after configuration. Domain/DNS verification and webhook
registration remain provider-side actions. Preserve existing webhook secrets or
enter the provider's Resend signing secret / matching Brevo bearer token.

## Explicit local pilot

Use a separate app with `decision.compare` and `knowledge.answer` grants, planning
quota and scopes `agents:read`, `agents:write`, `agents:run`, `tasks:read`,
`tasks:write`, `knowledge:read`, `knowledge:write`.

```sh
.venv/bin/python scripts/verify_workspace_capabilities.py \
  --client-file /absolute/private/client.json --live-model
```

This uses provider quota for a synthetic comparison plan and a cited answer. It
executes the read-only comparison through the worker, checks expected scores,
checks the synthetic document citation and removes its temporary document.
Saved task/agent records remain auditable. It sends no external messages and
places no telephone calls. Without `--live-model`, it only tests the checklist
and local document retrieval. The default console identity may already have an
active task; use the isolated app instead of interrupting that task.

Evidence: `docs/verification/capability-pilot.json`. A successful local pilot
does not certify WhatsApp delivery, carrier access, public webhooks or human audio.

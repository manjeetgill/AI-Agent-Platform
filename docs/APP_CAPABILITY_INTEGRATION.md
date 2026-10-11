# Consuming platform capabilities

The backend API supports multiple apps with separate service credentials and
server-derived `X-External-User-ID` values. Never put service credentials in a
browser, mobile bundle or source control. Permissions alone do not activate a
transport, grant tools or prove a real business task works.

## Operator setup

In **Connected apps**, add the required capability permissions, review the exact
scopes, select installed tools and set limits. The buttons add permissions only;
remove unwanted scopes in Application permissions before saving. Existing app
credentials are unchanged. Use operator credential rotation to add permissions to
an existing identity; creating a new app does not migrate its records.

Notification-only or WhatsApp-only apps can be created without task scopes or
tool grants. New UI selections start empty; choose only the capabilities needed.

Select an existing app and click **Check all capabilities**. This works for a
notification-only key as well as a task-enabled key. The report identifies missing
permissions, API paths, configuration and required live tests. Task execution has
its own check and diagnostic worker test.

## Backend contract

- `GET /v1/integrations/capabilities`: authenticated version-1 capability catalogue,
  required/missing scopes, API roots and setup requirements.
- `GET /v1/integrations/capability-readiness`: passive app/user-scoped configuration
  report plus `integration_contracts`. No sending, activation or paid probes.
- `GET /v1/integrations/task-readiness`: deeper worker/model/tool preflight.
- `POST /v1/integrations/connection-test`: optional idempotent worker round trip;
  requires the explicitly granted `platform.connection_test` tool.

Backend examples: `examples/integration/capabilities.py` and `capabilities.ts`.
They use bounded requests, reject redirects, do not print credentials and do not
retry uncertain writes. A failed preflight should disable the affected feature in
the app, not prevent the app from starting.

## Business workspaces

`POST /v1/businesses/{business_id}/credentials` now accepts these feature names:

- whatsapp_support, whatsapp_notifications, whatsapp_calling
- internet_voice, telephone_voice, memory
- agent_tasks, knowledge, email, sms, api_feeds, schedules

The parent must hold every scope being delegated. Business provisioning still
requires channels:read/write on the parent under the existing provisioning
contract. Child identities and data remain separate. Tools and transport
configuration are not inherited. Email and SMS currently share notification
scopes; per-app transport configuration and limits must restrict their use.
Credential replacement retains the existing behavior of revoking older child
keys; update the consuming backend when rotating.

## Reading status correctly

ACCESS_REQUIRED means permissions are missing. CONFIGURATION_REQUIRED means
saved setup is incomplete. MOCK_ONLY means delivery is simulated. UNAVAILABLE
means configuration could not be read. VERIFICATION_REQUIRED means a real test
is still required; it is not production certification. Counts cover first pages.
The memory contract currently gives setup guidance, not a live memory acceptance
check. WhatsApp alert route/recipient setup must additionally be checked through
its dedicated alert APIs; the messaging status alone does not verify alert delivery.

## Remaining gates outside discovery

Each app must provide its real business tools and authorization. WhatsApp needs
number authorization/webhooks; WhatsApp calls also need calling eligibility.
Telephone needs an authorized carrier. Email/SMS need configured transports.
Single synthetic voice success does not certify microphone behavior or concurrent
capacity: the latest public concurrent voice test failed and needs further work.
No provider invoice has been reconciled. This change does not certify those gates,
change any live account or consume provider credits.

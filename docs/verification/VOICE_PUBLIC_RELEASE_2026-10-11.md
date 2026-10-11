# Public voice release verification — 2026-10-11

## Deployed

Runtime revision: `3a2f0e1feea792c6ba54a02266de2448fa1438ed` on `agents.myalgodesk.in`.
Migrations through `066_voice_specialists.sql` applied. API and media healthy.
MyAlgoDesk login remained HTTP 200. Scoped APIs reject missing credentials;
workspace, internal media controls and OpenAPI remain unavailable publicly.

Release tests: 479 Python tests and 34 JavaScript tests passed before the first
release. The subsequent bounded reasoning-deadline correction passed 25 targeted
tests. Source changes include billing reconciliation, controlled FAQ specialists,
local FAQ speech corrections, authenticated health telemetry, workspace monitor
routing, and repeatable media release/rollback handling.

## Verified and limits of evidence

- Public TURN: relay-only audio and data round trip passed.
- Two separate synthetic speech sessions returned the correct opening hours in
  audible audio over public HTTPS/WebRTC. These do not verify a human microphone.
- Cross-app reads returned 404; the third simultaneous call returned 429.
- Sahayak: its actual public signed calling readiness probe passed. No booking or
  patient mutation was made by that probe. The separate business-code fixture
  tests verify booking confirmation and isolation but are not live patient tests.
- Reach CRM: saved service credential authenticates (HTTP 200); there are zero
  WhatsApp channels. Its local API is stopped and the prior temporary database is
  absent. Restoring the saved October 6 backup into a separate pilot database is
  awaiting owner approval. Business/number authorization remains required.
- Capacity **did not pass**. Initial concurrent calls exposed a fixed 20-second
  reasoning deadline, now aligned to the configured turn deadline (maximum 35
  seconds for reasoning, leaving time for speech). The fresh run still had a
  reasoning timeout and an audio-turn failure. No repeated retry of an uncertain
  operation was performed. Further waves stopped; this is not production-scale
  certification. The host stayed healthy; captured media memory was about 77 MiB
  of its 512 MiB limit during the fresh test.
- A private public microphone invitation was opened in external Chrome for the
  owner's existing conversation. Human confirmation is still pending.

## Spending and reconciliation

User approved INR 100 total. Two isolated pilot apps were capped at INR 40 each;
the owner's existing Sahayak voice app received the remaining INR 20 cap.
Conservative estimation rates include headroom and are not invoice prices.
Pricing reference: https://ai.google.dev/gemini-api/docs/pricing (checked today).
Load-test credentials were revoked and synthetic agents paused after testing.

The load tests account for INR 39.492 in conservative estimates plus uncertain
holds, **not actual billed charges**. Unknown requests remain reserved until
review. No real provider invoice/usage export has been supplied or reconciled.
A content-free private usage export is in
`.runtime/voice/provider-usage-for-reconciliation.json`; it must be refreshed to
include the latest attempts when matching the provider statement.

## Remaining owner inputs

1. In the prepared Chrome voice page, accept audio processing, click Talk to AI,
   allow the microphone, say Hello, and confirm hearing a reply after the greeting.
   If the private invitation expires, request a fresh invitation.
2. Approve a separate Reach pilot restore, or supply its current database. Complete
   the business number's provider authorization/OTP when prompted.
3. Supply the real provider invoice and usage export privately. No API keys in chat.
4. Supply an authorized carrier/SIP connection privately: permitted number/caller
   ID, trunk/server details, credentials or IP allowlist, and authorization evidence.
   No enabled, carrier-verified connection exists; no telephone call was placed.

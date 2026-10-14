# Public voice follow-up — 2026-10-11

## Changes

The single-attempt, non-streaming Gemini voice path now reserves against the
actual serialized request, its configured output cap, and conservative framing
headroom. The exact prepared request is dispatched. Streaming, retries and
fallback paths retain their previous bound. Unknown charges retain their holds.
The reservation change was deployed as `3c823dc` before the following test.

Follow-up gateway diagnostics record only timeout, transport, or HTTP status;
they omit upstream text, URLs, credentials and response bodies. HTTP 503 maps to
provider backpressure. These diagnostics cannot retroactively identify the HTTP
status or transport cause of an earlier generic `MODEL_UNAVAILABLE` event.

26 focused Python tests passed using disposable databases and restricted roles.
Ruff and diff checks passed. Tests cover matching reservations to dispatched
requests, preserving unknown holds, single attempts, and secret-free diagnostics.

The diagnostics release `e838029263a6479f0d5acac6854fd10759d53693` was
subsequently deployed with API, workers and media together. Public smoke checks
passed: MyAlgoDesk login 200, scoped APIs 401 without credentials, and private
surfaces 404. See `voice-public-soak-release.json`. No additional paid call was
made after that deployment.

## Public test: incomplete, not a capacity pass

The requested run was six waves of two simultaneous synthetic callers, 105
seconds per call. It stopped after the second wave (215.06 seconds):

- Three calls returned the expected spoken opening-hours answer over public
  HTTPS and relay-only WebRTC. Received audio was transcribed locally to check it.
- One call failed during Gemini reasoning with `MODEL_UNAVAILABLE`.
- Third-call rejection (429) passed in both waves. Cross-app reads returned 404
  for the three successful calls.
- First reply audio for the successful calls: 8,552 ms, 5,193 ms and 7,491 ms.
- No failed call was retried. The six-wave sustained public acceptance gate
  remains open. These synthetic fixture calls do not certify real business tools
  or a person's microphone.

Evidence: `voice-public-soak.json` and the latest per-app audio reports.
All four synthetic agents are disabled, no synthetic calls remain active, and
both temporary credentials were revoked.

## Budget and external gates

The approved aggregate cap remains INR 100 across the same three apps. Only the
two synthetic allocations were rebalanced to 33/47; Sahayak's 20 was unchanged.
After testing, accumulated estimates are INR 33.873 and unresolved holds are
INR 30.520: INR 64.393 consumed against the cap. No hold was discarded. This is
not a provider invoice. A content-free ledger export is stored privately under
`.runtime/voice/provider-usage-for-reconciliation.json` for later reconciliation.

The owner confirmed that neither an invoice/export nor an authorized carrier is
available yet. Real billing reconciliation and telephone activation remain
pending. Reach WhatsApp remains blocked by Chakra's authorization UI: a fresh
Chrome check stalled at 20% and reported an internal null `_data` error. Its
expired signup was not renewed without a provider-state review.

For the human public microphone check, the owner can ask Sahayak's connected
WhatsApp agent to talk to the receptionist, open the private voice invitation,
allow the microphone, ask a read-only business question, and confirm the answer
was audible. No human microphone success is claimed here.

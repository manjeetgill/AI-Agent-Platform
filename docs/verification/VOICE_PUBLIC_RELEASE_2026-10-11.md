# Public voice release verification — 2026-10-11

Follow-up runtime release and ten-minute local load results:
[Signup recovery and voice diagnostics](RECOVERY_AND_VOICE_2026-10-11.md).
The deployment section below records the preceding release.

## Deployment

Runtime revision: `efb8e1ba5e967eb42bcae59b478ff3682b1ccb8f` on
`agents.myalgodesk.in`. API, task/channel workers and extracted media service were
released together. Migrations through 066 remain applied. Rollback source and
private database/configuration backups are retained on the server.

MyAlgoDesk login remained HTTP 200. Scoped platform APIs returned HTTP 401 without
credentials; private workspace, internal media controls and OpenAPI returned 404.
Other apps' settings and sender bindings were preserved.

Release changes include granular consuming-app capabilities/scopes, authenticated
integration contracts, voice-specific low-latency Gemini thinking configuration,
stage-specific safe voice diagnostics, accurate Sarvam transport-model metering,
and Chakra connection checks using Chakra rather than Meta credentials.

Validation: 57 targeted tests passed for the preceding capability/voice release;
16 additional targeted tests passed for Sarvam billing and Chakra connection
checks. Ruff and diff checks passed.

## Concurrent public speech: passed bounded pilot

`voice-public-capacity.json` records **four successful calls**, in two waves of
two simultaneous calls, over public HTTPS and TURN-only client transport.
Each returned the correct synthetic clinic hours in audible speech, which was
transcribed locally for verification. Cross-app session reads returned 404;
a third simultaneous session returned 429. Total test duration was 174.58 seconds.

Successful configuration: Sarvam ASR (`saaras:v3`), Gemini
`gemini-3.1-flash-lite` reasoning, Sarvam TTS (`bulbul:v3`). Explicit selection;
no automatic provider fallback or uncertain-action retry.

Gemini-only speech previously failed with ASR timeout and TTS provider quota.
The first Sarvam pair had one success and one pre-dispatch reasoning reservation
failure: the second pilot's retained uncertainty holds left insufficient headroom.
The same total approved budget was redistributed (30/50/20 INR); the complete
fresh test then passed. Unknown holds were not cleared.

The verified speech selection was applied through the versioned API to the
owner's existing Sahayak internet voice agent
`9c2cb1e3-c4eb-4298-96f2-76fa4e45418d`, now version 2. Other customers' agents
and platform-wide defaults were not changed. Configuration-update credentials
were revoked immediately. Synthetic test agents were paused and test keys revoked.

This is a two-call pilot with synthetic audio. It does not certify human
microphones, large-scale sustained capacity, carrier calls or WhatsApp calling
eligibility.

## Connected apps and live channels

### Sahayak

- Actual public signed business readiness callback passed after deployment.
- On 2026-10-11 11:53:50 UTC the owner's new WhatsApp message entered the actual
  SAHAYAK worker route. The reply reached SENT and the provider webhook recorded
  **delivered**, not just accepted. See `sahayak-live-whatsapp.json`.
- Connected sender ending 1610 passed a live Chakra credential/number lookup.
- No appointment or patient record was created by these checks. Booking
  confirmation/isolation fixture tests remain separate evidence.

### Reach CRM — Demo Organization

- Existing scoped service credential authenticates; Chakra onboarding config is
  available. There are still zero authorized WhatsApp channels for this app.
- The saved database backup was restored/migrated into a separate private copy.
  Its actual voice callback served its saved demo FAQ over signed public HTTPS
  from the deployed platform. Event echo, exact replay, changed-replay rejection,
  cross-app rejection and private root checks passed. See
  `reach-public-connection.json`.
- The business content is synthetic demo information from Reach's saved database,
  not a production customer record. The app's lexical retrieval answers “When is
  the office open?” but misses “What are your opening hours?” This is a Reach
  retrieval limitation, not evidence of a successful general-purpose FAQ flow.
- The user selected sender +91 82196 13387. Previous signup
  `51e5f383-d6ca-445a-a1e8-fdde1fe721f1` expired on October 6 without a recorded
  candidate plugin. Do not blindly create duplicate provider bindings.
- Chakra's admin and quick-start pages stall at 80% with a client-side null `_data`
  error. The owner confirmed the same problem. New-number authorization cannot be
  completed until the provider screen works and its saved setup is reconciled.
- The temporary voice-only tunnel and isolated local callback server were stopped
  after testing. The original Reach backup remains unchanged. No live Reach
  WhatsApp message or phone call is claimed.

## Spending and reconciliation

Approved aggregate pilot cap remains **INR 100**: pilot apps INR 30 and INR 50,
owner Sahayak app INR 20. Latest ledger: estimated charges INR 29.0994 plus
uncertain holds INR 25.128 = budget accounting INR 54.2274. These are conservative
estimates, **not a provider invoice**. Uncertain attempts stay reserved.

Sarvam rates were checked at https://www.sarvam.ai/api-pricing on October 11.
Pilot rate cards use conservative headroom, expire after one day and are not
customer billing prices. Private content-free reconciliation export refreshed at
`.runtime/voice/provider-usage-for-reconciliation.json`. No actual provider bill
has been reconciled.

## Remaining external steps

1. Restore access to Chakra's onboarding page, reconcile the expired Reach signup,
   and complete owner login/OTP/number authorization for the selected Reach number.
   Then verify Reach inbound reply and outbound delivery.
2. Confirm a human microphone conversation on the public voice page.
3. Supply real provider usage/invoice evidence for reconciliation before billing
   customers; renew verified rate cards deliberately when the pilot continues.
4. Supply an authorized carrier connection for telephone calling. No telephone
   call was placed and no arbitrary mobile number is enabled by this release.

# Signup recovery and voice diagnostics — 2026-10-11

## Released

Runtime revision `ce8bafafc7c7714a063dfe83a63f84817b434a30` deployed to
`agents.myalgodesk.in`, with migration 067 and all API/channel/task/media services.
Private rollback source and database/configuration backup retained on the server.
The local voice runtime on ports 4160–4162 was restarted and migrated too.

- Expired Chakra signup now reports a safe next action, preserves its original
  business identity, and requires an independent operator review of the actual
  provider state before renewal. One-use dispatch fencing prevents blind retries.
- Existing reviewed plugins are pinned. A duplicate unresolved signup for the
  same app/owner returns its saved session ID. The integration example supports
  saved-state continuation and explicit renewal without storing tokens.
- Voice diagnostics distinguish budget, rate configuration, provider credentials,
  quota/backpressure, timeout and invalid model output. Only safe codes are exposed.
- The dashboard shows actionable explanations. The public widget retains the
  failure reason when a call has already ended or the end request fails.

## Verification

67 Python tests passed using disposable databases with restricted API/worker
roles. Eight JavaScript tests passed, including terminal-call failure display and
signup recovery. Ruff, JavaScript syntax and diff checks passed.

Authenticated public smoke checks verified migration 067, the new error asset,
cross-app session denial, and Reach's expired signup returning `operator_review`
without secrets. Three temporary read-only verification credentials were revoked.
The deployment also confirmed private surfaces stay unavailable publicly and
MyAlgoDesk login remains available. See the two accompanying recovery JSON reports.

The aggregate approved paid pilot cap remains INR 100. No new paid speech/model
calls were made by this follow-up; provider invoices remain unreconciled.

## Limits and external dependencies

Reach's saved signup was not renewed: Chakra's earlier provider outage remains
unresolved evidence, so there is no justified provider-state review to consume.
The owner still completes provider authorization/OTP once that works. Existing
consuming-app frontends must adopt the updated connection component/proxy routes.

Human microphone confirmation, real provider bill reconciliation, Reach live
WhatsApp delivery, and authorized carrier telephone activation remain separate
acceptance gates. These changes do not enable an arbitrary telephone number.

## Ten-minute local audio load test

The first attempt stopped before calls because migration 067 had not yet been
applied to the local runtime. Its failure is preserved in
`voice-soak-preflight-2026-10-11.json`; it is not counted as a successful run.
After migrating and restarting the local pilot, the sustained run passed:

- 56 successful synthetic audio calls, 28 waves, two concurrent callers.
- 603.97 seconds total; two isolated fixture apps.
- Third-call overload rejection and cross-app session denial passed.
- End-of-input to first received reply audio: p50 1,681 ms; p95 3,042 ms;
  p99 5,221 ms. These figures cover this local fixture only.
- 56 synthetic agents disabled; zero active sessions or unrevoked test keys.
- Zero non-local speech/model provider calls. No pilot budget increase.

Full evidence: `voice-soak.json`. This is local synthetic testing, not a public
microphone test, a carrier test or production-scale certification. The preceding
public four-call test remains the current public-network capacity evidence.

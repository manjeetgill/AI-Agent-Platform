# DealAgent live verification

## Repairs and manual checks on 9 October 2026

The original pass below is retained as history. Follow-up work found and fixed:

- **Robots agent matching:** DealAgent's parser matched arbitrary substrings in
  the full HTTP User-Agent. Vijay Sales' `es` crawler rule accidentally matched
  `myalgodesk.in` in our contact URL. Matching the crawler product token fixes this
  without disabling robots checks. See [RFC 9309 section 2.2.1](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.1).
- **JioMart:** replaced the dead `/p/groceries/...` probe with the `/product/...`
  page found through the site's search; added recognition of the new route.
- **Zepto / Instamart:** Chrome redirected to `www.zepto.com` and `instamart.in`.
  Added the observed canonical domains and updated configured probe URLs. Instamart
  now returns a real HTTP page and stops at the required location gate.

Vijay Sales now has a successful real worker check:
`7616cdd6-fefb-46df-8f32-278f6e6d1d4a`, HTTP 200, INR 150,999, fees unknown,
observed at `2026-10-09T02:36:45.353Z`. This matches the browser-displayed price.
Reliance Digital's earlier successful result remains valid as a dated observation.

| Source | Manual browser result | Remaining limitation |
|---|---|---|
| Croma | Product page displays INR 67,990 | Selected specification controls conflict with title/specifications; flagged `variant_conflict`. HTTP still denied. |
| JioMart | Current 100 g product page displays INR 62; delivery area confirmed in page | Details table says 500 g; flagged `variant_conflict`. Robots request returns 403. |
| Zepto | Fresh cream 1 L page displays INR 250 | No selected delivery location; worker receives JavaScript shell. |
| Instamart | Butter 100 g × 4 displays INR 252 | Delivery location unconfirmed and description says pack of 2; both warnings retained. |
| Blinkit | Chrome navigation blocked by client | HTTP robots request also returns 403; no price captured. |
| BigBasket | Browser displays Access Denied | HTTP also denied; no price captured. |

Four browser records are privately stored for the existing DealAgent application
and its mapped owner. The new [reviewed evidence tool](REVIEWED_BROWSER_EVIDENCE.md)
makes them available through durable worker tasks, explicitly as stored browser
observations. Location/variant uncertainties prevent their automatic acceptance
for exact comparisons. These records do not overwrite the catalogue or falsely
turn blocked HTTP connectors green.

[Follow-up worker results](verification/dealagent-repairs-2026-10-09.json) contain
the successful repair check and all four completed manual-evidence tasks:

- Croma: `7f6053cc-2d33-48bc-879e-1df2c7d4abde`
- JioMart: `46887eb6-1f6c-40fe-ab82-a748d0505229`
- Zepto: `16b73c5d-8ff7-40a3-a509-56c17cb8feb7`
- Instamart: `8b4197f0-dd75-4753-b24a-d5e9e587ea89`

Their `DONE` task state means the evidence read completed. Their
`usable_evidence=false` reflects the recorded uncertainty; it must not be ignored.

No CAPTCHA was bypassed, browser protection disabled, credentials obtained or paid
feed purchased. No recurring retailer checks were activated. **Two automated
sources now return usable prices; this is not eight unattended production feeds.**

Validation after these fixes: 87 Python integration tests, 23 DealAgent HTTP/robots
tests and 66 retailer fixture tests passed. Real network outcomes are separate
from fixture tests. API and workspace readiness both returned HTTP 200 after reload.

## Original pass executed locally on 9 October 2026

All eight original remaining retailers were checked through the platform API and
its durable task worker. The optional `dealagent.live_probe` v1 tool is installed
locally and granted only to the existing DealAgent application. Other apps' settings
and DealAgent's source checkout were preserved.

| Retailer | Result of this pass | Task ID |
|---|---|---|
| Croma | HTTP 403; no price | `8d0f5c0f-85b2-4b1b-bf72-15767e17f266` |
| Reliance Digital | HTTP 200; validated INR 70,990; fees unknown | `fab6c968-bd01-4576-a3cd-ccca7356ba5a` |
| Vijay Sales | Stopped at robots gate; product not requested | `3a0349ab-4a33-47a9-bbd8-de112f2737b0` |
| JioMart | Stopped at robots gate; product not requested | `4a55c019-719d-4714-9cce-4d3b1748d713` |
| Blinkit | Stopped at robots gate; product not requested | `4ed0fb13-d670-46df-bb00-9b23c77d0b6d` |
| Zepto | HTTP 202 JavaScript shell; price/location unverified | `dcb90cdb-cc81-4cb8-bf61-c81282ff5423` |
| Instamart | HTTP 202 JavaScript shell; price/location unverified | `c0e8df11-7767-4c5d-acf2-6f726f10d9c8` |
| BigBasket | HTTP 403; no price | `85233370-6068-48ed-8b28-232d4aaafcfa` |

[Machine-readable evidence](verification/dealagent-live-2026-10-09.json) includes
source URLs, UTC observation/check times, request counts, parser versions, report
IDs, task IDs, prices when validated and unknown-fee flags. The price was observed
at **02:22:09 UTC**; this does not guarantee availability, delivery or checkout total.

This pass completed eight checks, with one usable price result. It does not mean
eight working production data sources. The robots gate can fail closed when its
policy cannot be retrieved; this pass did not retain the robots HTTP status, so
those three results are not proof of an explicit disallow directive. Subsequent
checks record `robots_http_status` and distinguish policy refusal from network or
server failure. No source was retried around an access refusal.

An initial adapter www-host mismatch failed locally for seven sources. Those
seven were rerun after the fix and their persisted cooldown; the original failures
remain in the audit log. Blinkit's already-recorded refusal was reused.

## Installation and tool contract

Set identical operator configuration for API and worker:

- `DEALAGENT_PROBE_ROOT`: absolute path to the trusted DealAgent checkout, containing
  `engine/src/crawler`, `engine/retailers` and installed `engine/node_modules`.
- `DEALAGENT_PROBE_NODE`: absolute path to Node 22+.
- `AI_AUTOMATIONS=1`, the ordinary durable-task flags and migrations through 042.

The operator grants `dealagent.live_probe` to the intended app. The direct tool
internally uses `runtime.connector_check` for admission, leases, cooldowns, report
persistence and cancellation; the broader runtime grant is only required for
explicit wrapper invocations. It does not import a consuming app's credentials.

```json
{
  "tool": "dealagent.live_probe",
  "version": "1",
  "arguments": {"retailer": "reliancedigital"}
}
```

Allowed retailer identifiers are croma, reliancedigital, vijaysales, jiomart,
blinkit, zepto, instamart and bigbasket. URLs and parser configurations come only
from the operator's DealAgent checkout. Callers cannot submit arbitrary URLs,
commands, paths or executable arguments. `pincode` is optional; supplying one does
not implement or confirm a location flow. The source circuit remains retailer-wide
for that app/owner even when pincode changes.

The bridge reuses DealAgent's HTTP client and deterministic extraction/validation.
It never starts its browser, bypasses a challenge, signs in, purchases, changes the
collection queue or writes into DealAgent's catalogue. It reads the configured
public product only after its robots gate permits it. HTTPS destinations are
limited to the configured hostname and its www variant. HTTP requests are bounded,
with no immediate retry, a five-second request timeout, an eight-request cap
(including robots/redirects), a 2.5 MB response cap and a 17-second child lifetime.
No platform/model/API secrets or proxy configuration are inherited by the child.
Revocation terminates the child; schema-invalid output cannot become evidence.

Browser/location-dependent sources return setup requirements. A validated product
price requires the existing parser and price gate to accept it. Missing fees remain
unknown. Fresh successful observations use the actual capture timestamp and a
stable observation hash, rather than an LLM-generated amount.

Save a one-step agent and schedule it through the existing automation API. The
same owner has one active task at a time, each source has a lease and cooldown,
and the application-wide check quota still applies. Blocked sources stay stopped
until an operator resolves access and explicitly resumes the latest report.
For denied sources, configure an approved API/feed alternative; HTTP 202 shells
need investigation and an authorized rendering/location adapter before prices can
be used. This bridge does not promise to resolve those third-party prerequisites.

## Repeat or resume the bounded pass

```bash
.venv/bin/python scripts/verify_dealagent_live.py \
  --client-file /absolute/private/dealagent-client.json \
  --output .runtime/local/dealagent-live-checks.json
```

The private client JSON contains url, app_id, credential and external_user.
Credentials never appear in output. Rerunning with the same output file resumes
saved task IDs and idempotency keys; it does not submit completed checks again.
Use a new output filename for a deliberately new pass. Recorded source blocks and
cooldowns are still enforced. This command creates no recurring schedule or paid
model request.

Use repeated `--retailer NAME` options for a targeted verification after a specific
repair, rather than requesting all blocked sources again.

Validation: 84 Python integration tests passed (real disposable PostgreSQL, APIs
and workers); 11 Node probe tests passed; lint and JavaScript syntax checks passed.
Actual live results are the separate eight task records above.

# Retailer verification: server execution and optional paired browser

## Default flow for mobile and web apps

Use **`retailer.observe` v1** for public retailer checks. The app calls the platform API; the task and renderer workers do the work on platform infrastructure. The end user needs **no extension, laptop, or open browser tab**. Task progress and results stay available through the same API after the app is closed.

Use the existing HTTP probes and authorized feeds where available. Public browser rendering is another server transport, not a guarantee of access: a retailer can still block cloud traffic or require a confirmed delivery area. Those cases return structured failures/unknown evidence, never invented prices.

`browser.retailer_observe` is an optional personal-browser path only when a user explicitly chooses it. It must not become a required onboarding step for DealAgent or any other consuming app. Tool discovery now includes `execution_location` and `requires_customer_browser` for these two tools.

### Server setup

Apply migration `044_server_retailer_browser.sql` after migration 043. The existing task worker starts the renderer loop when `AI_RETAILER_SERVER_BROWSER=1` alongside the durable-task, automation and retailer-browser flags. It admits one renderer globally by default; `AI_RETAILER_SERVER_MAX_CONCURRENCY` can raise the cap to two. Per-app quotas and connector limits remain in force.

Install the platform's own optional runtime with `npm ci --ignore-scripts` in `runtimes/retailer-browser`, then install Chromium using that pinned Playwright CLI. Set these operator-only values privately:

- `AI_RETAILER_NODE`: absolute Node executable path.
- `AI_RETAILER_PLAYWRIGHT_MODULE`: absolute `runtimes/retailer-browser/node_modules/playwright/index.mjs` path.
- `AI_RETAILER_RESOURCE_HOSTS`: optional JSON map of retailer to explicitly reviewed public script/style/API hostnames. The canonical retailer host is always included. No model or consuming app can change this allowlist.
- Optional `PLAYWRIGHT_BROWSERS_PATH`: installed browser binaries location.

Each render uses a fresh browser context with no customer cookies, saved login, shared profile, downloads, service workers or permissions. Chromium's sandbox and certificate validation remain enabled. DNS is checked for public IPv4 and pinned for that render; unlisted hosts, private IPs, non-HTTPS requests, POSTs, WebSockets and subframe navigations are blocked. Rendering is bounded by time and request count. The child process does not inherit app or model credentials.

For production, run the renderer under a dedicated unprivileged account/container with network egress restrictions and no application secrets or database volumes mounted. Browser request interception is not a replacement for host isolation. Do not put many Chromium processes on the existing small droplet without measuring memory use. This turn configures the local pilot; it does not deploy a new cloud browser service.

See [Playwright's context routing documentation](https://playwright.dev/docs/api/class-browsercontext#route) for why service workers are blocked during interception.

## Optional paired-browser flow

`browser.retailer_observe` v1 is a read-only, provider-neutral task tool. It supports canonical product URLs on Croma, JioMart, Zepto and Instamart. DealAgent supplies product URLs through the same task-agent API as other tools. It does not need the model to extract a price.

The task worker stores a job in PostgreSQL before Chrome receives it. A browser is paired to one application and external user for 24 hours. Its device secret cannot call other platform APIs and is invalidated when the originating service key or tool grant is revoked. Secrets are kept out of retailer pages, transcripts and audit payloads.

The extension opens at most one tab for an acknowledged job and reads fixed product DOM fields. No arbitrary JavaScript/selector supplied by a model is executed. It never logs in, enters passwords, changes delivery details, clicks purchase controls, submits forms or pays. Touching the page stops its read. The popup also offers Stop/take over, revoking the device and pausing its outstanding tasks.

Jobs expire after five minutes. Waiting is durable and uses the existing external-job worker. A crash or uncertain tab creation never causes a second dispatch. A browser that is offline/interrupted produces a configuration-required report and stops automatic checks for that source until explicit resume. App-wide connector concurrency (3), hourly quotas and source backoff apply alongside HTTP/feed checks.

## Evidence and current limits

- Product title, price in INR, variant, product URL, observation time, verified postcode where available, and quality flags only. No page HTML, cookies, screenshots or account data are returned.
- Prices are product observations, not checkout totals. Fees remain unknown.
- Croma selected options are compared to visible specifications. JioMart title quantity is compared to its Net Quantity row. Instamart pack counts are compared with its product description.
- Missing or conflicting variants, stale timestamps, navigation to another URL, multiple price candidates, and an unconfirmed required delivery location make evidence unusable.
- JioMart postcode can be read from its visible delivery header and must match the request's expected postcode.
- **Zepto and Instamart do not yet expose a verified postcode selector in the pages tested. Their results stay location-unconfirmed; they are not usable local price feeds.** The runner does not assume a location from the requested postcode, seller address, or delivery estimate.
- Croma/JioMart examples tested also contain contradictory product details. These remain flagged.
- Browser results are authenticated reports from a paired client, not cryptographic attestations from the retailer. A compromised paired browser is outside that trust boundary.
- Blinkit and BigBasket access failures are not bypassed. Use the existing authorized API/feed framework if the source grants access. HTTP probes for Reliance Digital and Vijay Sales remain separate.
- This extension pilot connects only to `http://127.0.0.1:4060`. Hosted browser pairing needs an explicitly approved HTTPS API origin; arbitrary endpoints are rejected.

## Enable the optional paired browser for an application

1. Apply migration `043_retailer_browser.sql` with the normal migration command.
2. Set `AI_RETAILER_BROWSER=1`, `AI_AUTOMATIONS=1`, and the existing durable task/agent flags in private server configuration. Restart API and task worker together.
3. Grant only `browser.retailer_observe` to the application's existing identity. Keep its `agents:write`, `agents:run`, `tasks:read`, `tasks:write` scopes. Preserve other applications and grants.
4. Create a private device file:

```sh
.venv/bin/python scripts/pair_retailer_browser.py \
  --client-file /private/path/application-client.json \
  --output /private/path/retailer-pair.json \
  --retailer croma --retailer jiomart --retailer zepto --retailer instamart
```

5. Reload the unpacked `browser-extension` (version 0.2.0). Open its popup, select the private pairing file, and click **Allow selected sites and connect**. Chrome requests only the listed origins and localhost port 4060. The service API key stays on the backend; the extension receives a narrower device key.
6. Keep the popup open for fast polling. With it closed, Chrome's alarm checks every 30 seconds. Revoked/missing optional site permissions prevent dispatch to that site.

## Queue an observation

```sh
.venv/bin/python scripts/retailer_browser_task.py \
  --client-file /private/path/application-client.json \
  --state-file /private/path/croma-observation.json \
  --retailer croma --url https://www.croma.com/real-product/p/12345
```

The command defaults to **server execution**. Add `--paired-browser` only to opt into the extension.

Use a real URL discovered from the catalogue or source. Add `--expected-variant` and `--expected-pincode` only when known. The expected postcode is a comparison constraint, never an instruction to change the user's delivery area.

Repeat the command with the **same state file** to inspect the same task. It saves idempotency keys before network requests and never creates a replacement task just because the first is waiting. Results appear in `/v1/tasks/{id}` and `/v1/connectors/reports` with `report_id`, `usable_evidence`, `evidence_issues` and recovery guidance. No catalogue ingestion occurs merely because a browser reports a price; the consuming application must honor these flags.

After resolving an attention condition, explicitly call `POST /v1/connectors/retailer.observe/{retailer}/resume` for server checks, or `/v1/connectors/browser.retailer_observe/{retailer}/resume` for paired checks, with the latest `report_id`. Respect `next_check_at`, then start a new check. Resume does not erase the previous evidence or waive quotas.

## API

| Endpoint | Authority | Purpose |
| --- | --- | --- |
| `POST /v1/retailer-browser/devices` | App service key + external user | Pair selected retailers, revoke previous device for that user |
| `GET /v1/retailer-browser/devices` | Same app/user | List metadata without secrets |
| `DELETE /v1/retailer-browser/devices/{id}` | Same app/user | Revoke a device |
| `POST /v1/retailer-browser/poll` | Device key | Claim at most one job for granted sites |
| `POST /v1/retailer-browser/{id}/authorize` | Assigned device | Recheck saved action, active task and grant |
| `POST /v1/retailer-browser/{id}/result` | Assigned device | Save bounded observation once |
| `POST /v1/retailer-browser/stop` | Device key | Revoke and pause outstanding browser work |

## Verification

The PostgreSQL/API/worker suite covers separate app/user identities, paused tasks, device rotation/revocation, timestamp and URL validation, offline expiry without redispatch, permission subsets, evidence quality, cooldown/resume, and hourly quotas. Node tests use fixture DOM and mocked Chrome APIs; they do not launch a browser or contact retailers.

A real installed-extension run must be verified separately after the owner grants site access. The server API/worker path is tested separately with no device pairing. Fixture captures and manually inspected retailer pages are not a substitute for that end-to-end proof.


## Local live server results — 2026-10-09

The real API and worker ran two `retailer.observe` tasks with no customer browser pairing:

- Croma: task `1d2c3137-7bd0-41fe-98fa-741b2c02ab1e`, DONE with a BLOCKED report (`browser_blocked`). The source requires attention and automatic rechecks stopped.
- Zepto: task `6f5918ad-abd3-483f-9395-928e7e1e7646`, DONE with a BROKEN report (`browser_page_changed`). The isolated renderer did not obtain parseable product evidence; source backoff applies.

These demonstrate real server execution and honest failure handling, **not successful price collection**. No price was accepted from either check. Reliance Digital/Vijay Sales HTTP evidence remains documented in `DEALAGENT_LIVE_CHECKS.md`. The extension pilot was not verified end to end: its API did not receive a device poll before the queued test expired.

Non-secret task results are in `verification/dealagent-server-browser-2026-10-09.json`.

## Local-only integration handoff — 2026-10-09

Public deployment is outside the current scope. Keep the API at `http://127.0.0.1:4060`; DealAgent's backend calls it using its existing private application credential. No extension is required. The existing DealAgent app identity and all other applications' settings are preserved.

From the platform repository:

```sh
.venv/bin/python scripts/local.py start
.venv/bin/python scripts/local.py status
```

`start` applies checksum-verified migrations and supervises the API and worker together. A second launcher is refused. If either child exits, its companion stops and an actionable restart message is shown. Ctrl-C leaves PostgreSQL and saved reports intact. The worker handles termination by cancelling its renderer and cleaning up its subprocess. `status` distinguishes API/database readiness from a fresh local supervisor report; this is process health, not a claim that every external provider works. Keep the Mac awake while jobs run.

The configured DealAgent application has these granted tools:

- `dealagent.catalog_search` v2, `dealagent.product_observation` v1
- `dealagent.collection_request` v1, `dealagent.coverage_check` v1
- `dealagent.live_probe` v1, `runtime.connector_check` v1
- `retailer.observe` v1 (default server execution)
- `browser.retailer_observe` v1 (optional), `evidence.reviewed` v1, `runtime.next` v1

Its existing scopes are `agents:read`, `agents:write`, `agents:run`, `tasks:read`, `tasks:write`, and `tasks:approve`. Its key remains in DealAgent's private `data/agent-platform.json`; never put that key in frontend code or a phone browser.

Use `GET /v1/task-agents/tools` to discover typed inputs and outputs. Submit natural language to `POST /v1/task-agents/interpret`, save its reviewed blueprint through `POST /v1/task-agents`, and run via `POST /v1/task-agents/{agent_id}/runs`. Persist the task ID and retrieve `/v1/tasks/{task_id}` when the app reconnects. Keep the same idempotency key after a timeout. Connector status and evidence are available through `/v1/connectors` and `/v1/connectors/reports`.

### Verified platform boundary

- Fresh configured NVIDIA model request “Find Samsung TVs under ₹60,000.” produced `dealagent.catalog_search` v2 with query `Samsung TV` and `max_price=60000`.
- Real worker task **`976a2e3a-eb79-4a78-ac14-3806b45f65bc`** completed in one attempt, after the initiating HTTP client disconnected. It returned catalogue records containing retailer URLs, observation timestamps, prices and explicit unknown fees.
- **Product relevance failed:** the DealAgent tool returned remotes/speakers, not televisions. Worker DONE means execution succeeded, not that the shopping request is satisfied. DealAgent must fix category/brand/accessory matching and rerun shopping acceptance. The platform does not rewrite observed products or invent replacement prices.
- Zepto diagnostic task **`90e961b1-b9c3-4ad6-a18a-cb8441a66996`** returned HTTP 202, two requests, one blocked resource and no product evidence. It now reports `CONFIGURATION_REQUIRED / browser_resource_policy`, with automatic retries stopped for operator review. Counts do not prove that allowing the blocked resource would solve the issue. Do not relax access controls or treat challenge pages as product observations.
- Server reports now include bounded diagnostic counts and HTTP status, without raw page text, request URLs, headers, cookies or credentials. Croma remains blocked; missing retailer evidence remains missing.
- Verification: 107 PostgreSQL/runtime tests, 5 local lifecycle tests, and 27 Node policy/adapter tests passed. Node tests use mocks/fixtures; the task IDs above identify actual execution.

### Remaining work in DealAgent

1. Correct product/category matching in its catalogue search tool.
2. Consume the existing task/progress/result APIs in its UI, including `usable_evidence`, stale/variant/location issues and unknown fees.
3. Supply maintained retailer adapters or authorized feed credentials when public access is blocked.
4. For same-Wi-Fi phone testing, add an explicit local LAN/session policy to DealAgent. Its current `requireLocalAgentUser` accepts only localhost/loopback hosts, so opening the Mac's IP alone is insufficient. Keep platform credentials on DealAgent's backend; do not expose port 4060 to solve that UI constraint.

Repeat the platform execution check without creating duplicate tasks:

```sh
.venv/bin/python scripts/verify_shopping_local.py \
  --client-file /private/path/agent-platform.json \
  --state-file .runtime/local/shopping-check.json
```

Reusing the state file resumes the same check. Use a new state file only for an intentional new test after fixing the catalogue adapter. The command verifies execution; product relevance still requires acceptance review.

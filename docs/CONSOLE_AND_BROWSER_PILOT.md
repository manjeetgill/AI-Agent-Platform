# Local console, live tools and Chrome pilot

## Start

```sh
.venv/bin/python scripts/console.py
```

Visit `http://127.0.0.1:4061/`. This launches the API and durable worker using existing private
NVIDIA settings. The console uses a separate `agent_console` database in the platform's local
PostgreSQL cluster; it does not reset or edit existing research reports. First startup creates
and migrates that database. The existing research database currently has a checksum mismatch
for migration 006; this pilot deliberately leaves it unchanged. Reconcile that historical
migration before upgrading the research database—never overwrite migration checksums blindly.

The console is for one trusted local operator. It rejects non-loopback clients, unexpected Host
and Origin values, and requests without the console intent header. Service credentials remain
server-side and are revoked on graceful shutdown. This is not hosted multi-user authentication;
do not bind or reverse-proxy the console onto a public network. Use the authenticated service API
and your application's own identity layer for a hosted product.

Request, draft review, save/run, exact-action approval, task controls, results, saved-agent history
and recent tasks are connected to the existing APIs. Rendered model/data text uses `textContent`.
Task IDs and invocation keys persist locally so reload can restore observation without rerunning
a task. API/model/worker failures remain visible; closing a browser tab does not cancel a task.

## Live verification performed

- The configured NVIDIA `nvidia/nemotron-3-ultra-550b-a55b` generated a valid comparison agent from
  the console's sample request. Saving, running and viewing results completed: A=8, B=7.
- It generated a weather agent for explicitly supplied Berlin city-centre coordinates.
- The real Open-Meteo adapter returned a two-day forecast; the task reached DONE and its results
  survived reloading the console. No personal coordinates or account details were used.
- Request/review/results were inspected through the in-app browser. Approval enforcement and
  browser command fencing are covered by API tests; Chrome extension installation is pending
  the manual browser step described below.

## Real tools, added incrementally

`decision.compare` computes a deterministic weighted comparison from supplied inputs.
`weather.forecast` calls only `https://api.open-meteo.com/v1/forecast`, has bounded typed coordinates
and duration, a response-size limit, timeout and no redirect following. It returns source,
retrieval time, timezone, temperatures and precipitation probabilities. It never requests location
permission or infers personal location. See [Open-Meteo API documentation](https://open-meteo.com/en/docs).
Before commercial deployment, review the provider's commercial-use requirements and capacity.

## Install and pair Chrome

1. In Google Chrome, open `chrome://extensions` manually and enable Developer mode.
2. Choose **Load unpacked**, then select this repository's `browser-extension` directory.
3. Open the local console. Click **Connect Chrome runner**; reveal/copy its pairing code.
4. Open **Agent Workspace Browser Runner** from Chrome's extension menu. Paste the code and
   click **Connect**. Do not share the code in chats, screenshots or issue reports.
5. Keep the extension panel open while waiting for approved work. Background alarm polling is
   slower; a sleeping/offline browser can leave a dispatched task waiting for reconciliation.
6. Request “Prepare a Wikipedia search for Python programming, without submitting it.” Review,
   save and run. The task must wait for an explicit **Yes, approve this exact action**.
7. After approval, the runner opens Wikipedia and fills only its known public search field.
   It does not press Enter or click Search. The user controls the final submission.
8. **Stop / take over now** in the extension stops locally first, then requests server pause.
   Trusted keyboard/pointer input on the page also stops filling. The console's **Pause task**
   revokes server authority; use the extension Stop button for immediate local takeover.

The browser automation environment blocks navigation to Chrome's extension-management page,
even after user approval, and forbids alternate automated routes. Consequently the extension
cannot be installed by this coding task; that one installation step must be completed manually.
After installation, continue the live Chrome test. Do not describe the extension as live-verified
until an actual Chrome command and result have been observed.

## Browser boundary

The Manifest V3 extension requests host access only to localhost:4061 and www.wikipedia.org,
plus storage, scripting and alarms. It uses an isolated-world guard installed at document start.
It has no cookies, debugger, arbitrary host access, payment action, screenshot collection,
credential-field reader or generic script executor. Query text is a literal string, never code.
The fixed site adapter rejects page changes and pages containing password, OTP or card inputs.
Source references: [isolated scripting](https://developer.chrome.com/docs/extensions/reference/api/scripting)
and [service worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).

Pairing secrets are hashed in the database, expire after 24 hours and are replaced on re-pairing.
The device can retrieve only its app/user's browser commands, authorize the assigned command,
return bounded observations, or pause that user's active tasks. It cannot approve spending,
create agents, invoke arbitrary tools or access another user's browser session.

Commands are persisted before dispatch, bound to an attempt lease, and marked STARTED once.
Expired/stopped commands fail authorization. Late results are retained for reconciliation and
cannot directly finish a task. Pending commands that never started can be verified as not
applied. A started command without a definitive observation stays uncertain; this pilot does
not yet have a general site-state reconciliation reader. Never blindly resubmit it.

## Tests and limits

Platform suite: 53 tests passed in the verification run. JavaScript runner policy suite: 2 tests
passed, covering expired/unsupported commands, wrong origins, secret-field refusal and takeover.
Tests include console cross-origin/Host rejection, pairing rotation, one-time dispatch, revoked
leases and late observations. Live browser runner installation/Chrome execution remains pending.

This is a local pilot. Browser capability is limited to one public search-preparation adapter.
General form filling, merchant integrations, payment screens/QR validation, receipts, secret-free
login handoff, device reconnection reconciliation and hosted multi-user deployment need separate
site adapters and end-to-end validation. No live payment or external message was performed.

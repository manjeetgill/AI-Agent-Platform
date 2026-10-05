# Droplet pilot status — 2026-10-05

The independent platform is installed under `/opt/agent-platform/pilot` on the
existing Droplet. Compose project: `agent-platform`. Run with `compose.yaml` and
`deploy/compose.small.yaml`, profile `whatsapp`. Production configuration is in
the root-owned, mode-0600 `.env`; never print it or commit it.

The image built successfully. Migrations completed and the private API readiness
check at `http://127.0.0.1:4060/readiness` passed. API, task worker, WhatsApp worker,
Meta worker and a separate PostgreSQL instance are running. The operator console
is not deployed or publicly exposed. This is a pilot, not a public release.

The database, signing key and encryption key are new for this deployment. Local
test channels, customer data and old test access tokens were not imported.
NVIDIA and Meta app credentials were transferred privately. The main app's agent
integration has not been enabled against this new instance.

## Remaining gates

- HTTPS is installed and verified for `agents.myalgodesk.in` (HTTP 200, certificate
  validation successful). Public `/v1/agents/catalog` returns 404. The main app
  remains HTTP 200. `deploy/install_https.py` installs a separate proxy overlay,
  leaving immutable main-app release files untouched. **Run this installer again
  after each main-app deployment**: the existing main-app controller does not yet
  preserve this overlay automatically. Automate that release hook before public
  launch. The shared proxy restarts briefly when applying the overlay.
- Create the pending Meta Embedded Signup configuration after the requested
  specific user approval, then inject its configuration ID.
- Configure and verify Meta's shared webhook on the permanent HTTPS origin.
- Connect a real authorized number and verify inbound events, agent processing,
  outbound delivery and reconnect/disconnect through the platform. The prior
  Hello World delivery tested Meta's dashboard only.
- Complete required Meta permissions/review for public customers. No business
  registration or verification is claimed for the individual developer.
- Add off-host encrypted backups and a restore check before relying on stored
  customer data. Preserve the encryption keys independently of database backups.

Memory use observed after startup: approximately 240 MiB for the new services;
the host had approximately 699 MiB available. These are idle measurements, not a
concurrency/load test or a guarantee of production capacity.

## Operations

From `/opt/agent-platform/pilot`, use:

```sh
sudo docker compose -p agent-platform -f compose.yaml -f deploy/compose.small.yaml --profile whatsapp ps
curl --fail http://127.0.0.1:4060/readiness
```

Do not run `down -v`. The separate database volume is `agent-platform_agent_data`.
Keep `.env` and that volume together across application updates. The pilot image
is `agent-platform:pilot`; introduce immutable release tags before routine releases.

## Chakra pilot progress

The user selected ChakraHQ for the WhatsApp connection. The new NRAIAlgo WABA
and sender ending 1610 are connected; a read-only API check returned HTTP 200
and number verification VERIFIED. A dedicated Bot-role API credential is stored
root-only under `/opt/agent-platform/secrets/chakra-token`. Chakra assigned it an
expiry in October 2031; shorten/rotate it through supported controls before public
launch. No credential appears in source.

The Chakra adapter is deployed. The callback is configured and a real inbound
message passed HMAC verification on 2026-10-05. The isolated pilot channel is
enabled. Template catalogue/multi-recipient integration remains gated. Fourteen
focused adapter tests passed. Live end-to-end reply delivery is still being checked.

NVIDIA returned HTTP 503 during reply generation. Gemini fallback is now deployed
for explicit HTTP 429/503 only; transport timeouts and invalid output do not trigger
cross-provider repeats. Google directed this account to gemini-3.8-flash, and a
live generation check succeeded. The key is stored privately on the server. Ten
gateway tests passed, including credential origin isolation and schema validation.
The failed test message had no outbound dispatch. Operator recovery attempts
use separate generation keys and retain the original failed generation record.
Both bounded real FAQ recovery attempts returned Gemini HTTP 503; no reply was
queued. Gemini fallback is deployed but end-to-end WhatsApp reply delivery remains
blocked on successful model generation. Do not reset uncertain sends or claim delivery. Direct Meta Embedded Signup is an
alternative path and is no longer the immediate pilot prerequisite.

## Verified end-to-end pilot — 2026-10-05

The newly supplied Gemini key and gemini-3.5-flash-lite successfully generated a
source-grounded answer for the saved pilot message. The guarded operator recovery
preserved prior generation attempts, checked no outbound dispatch existed, and
queued the reply once. The worker recorded SENT and Chakra/WhatsApp returned a
signed `delivered` status. Evidence records provider `gemini` and model
`gemini-3.5-flash-lite`. The reply explained the private FAQ test and that no trades
or payments are performed. This supersedes the model-blocked status above.

The full pilot path (inbound signature verification → FAQ retrieval → model
fallback → Chakra send → delivery event) is now verified. Public customer launch,
additional customer onboarding, template multi-send, and the HTTPS release hook
remain separate unfinished work. Credentials and message IDs are not recorded here.

## Multi-business integration revision — 2026-10-05

Added migration 019, backend-only business workspace/credential APIs, saved Chakra
signup sessions, encrypted owner-bound connection storage, guarded webhook setup,
and local disconnect controls. Business data uses a separate app boundary, so
app-shared FAQs cannot leak between customer businesses. Nineteen focused
PostgreSQL integration/regression tests passed; Chakra network calls in onboarding
tests are simulated, not evidence of a second live customer signup.

The current Chakra config response lacks clientReferenceId. New customer binding
therefore requires independently verified operator approval. The browser callback
never grants ownership. Partner onboarding remains disabled until authorized
partner credentials, shared HMAC and graph-version settings are supplied. Public
API ingress and the integrating app's authenticated backend/UI must be configured
for the chosen application. See docs/WHATSAPP_APP_INTEGRATION.md.

## Sahayak local clinic pilot — 2026-10-05

User authorized using sender ending 1610 for the clinic test. Chakra callback was
changed to `https://shayak.myalgodesk.in/webhooks/whatsapp/ed9f35a0-3167-4e56-bc94-340f4cce3164`
and read back successfully. The same channel now has execution_mode SAHAYAK and
was enabled through the normal activation endpoint after the signed probe passed.
The old FAQ documents are retained for rollback. No queued/uncertain sends existed
at migration. Platform migrations 020/021 and updated API/WhatsApp worker deployed;
rollback image is `agent-platform:before-clinic`, config `.env.before-clinic`.
Eighteen focused platform tests passed before deployment.

This is a **local clinic test**, not a deployed Sahayak application. The signed
HTTPS `/integrations/agent-platform/whatsapp` route reaches a private relay at
172.24.0.1:44001, then an SSH reverse tunnel at server loopback 44000, then the
local Sahayak API on 4000. The relay systemd unit is `sahayak-pilot-relay` (started,
not enabled at boot). UFW allows only Caddy's current private IP 172.24.0.3 to the
relay; update this rule if that container address changes. Local scheduled jobs
are disabled and outbound channel ownership is platform-only. The laptop and
SSH session must stay running. Tunnel failure fails closed; uncertain processing
is not blindly retried. No public staff/doctor/API endpoints are exposed.

Live phone delivery through this clinic workflow is awaiting the user's Hi.
The signed empty-event test at the new hostname returned 200 and unsigned 403;
that test does not establish provider delivery.

To stop the pilot: pause the channel using its activation API first, then stop
`sahayak-pilot-relay` and close the SSH reverse tunnel. Never reset unknown sends.
The Caddy hostname/route is stored in deploy/agents.Caddyfile. Its installer needs
a proxy restart when the mounted file changes (admin API is disabled).

Proxy follow-up: fixed install_https.py to qualify the main API upstream as
myalgodesk-api-1:4020 (both stacks publish the Docker alias api) and restart Caddy
after a mounted config change. Verified main /v1/auth/me returns expected 401
without credentials, and public agent /v1 remains 404. This supersedes the manual
restart note above.

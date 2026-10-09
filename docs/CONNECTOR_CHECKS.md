# Connector verification for consuming applications

This is platform infrastructure for checking operator-installed connectors. It
contains no retailer scraping or affiliate credentials and makes no assumption
that a blocked source is accessible. Apply migrations through **041**, then restart
API and worker with `AI_AUTOMATIONS=1`.

## What is available

- `runtime.connector_check` v1: a READ tool run by the durable worker.
- Common results: WORKING, BLOCKED, RATE_LIMITED, LOGIN_REQUIRED,
  CONFIGURATION_REQUIRED, BROKEN, UNAVAILABLE.
- Observed URLs, IDs, timestamps, optional price/currency, stale/missing/future
  evidence flags and explicit unknown-fee indicators. Connectivity and usable
  evidence are separate: WORKING with no required observations is not a usable
  price result. Unknown fees do not mean zero or a verified all-inclusive price.
- Persistent reports, per-source cooldowns, hourly app quotas and three concurrent
  admitted checks per app. A new task/argument does not bypass its source cooldown.
- Existing scheduled automations, notifications and signed event webhooks.
- App/owner authorization and forced RLS for report/state tables. Database roles
  must be non-superuser and lack BYPASSRLS for RLS to protect production data.

The worker validates supplied evidence and freshness. It does not independently
prove a retailer's claims or use an LLM to fabricate missing prices.

## Adapter contract

Add a read-only `health_check` method to an operator-installed typed READ tool.
The method takes the same prepared action used by that tool. It must check
`authorized()` immediately before each remote request, honor cancellation and its
own destination allowlist, use private server-side credentials, and return a dict
matching `agent_platform.connector_health.ProbeResult`:

```python
async def health_check(self, action, *, task_id, authorized):
    if not await authorized():
        raise PermissionError('Revoked')
    # Perform a bounded read through your existing allowlisted client.
    # Translate provider errors explicitly; do not return raw error/page bodies.
    observation = await self.client.read_observation(action.arguments)
    return {
        'status': 'WORKING',
        'reason_code': 'observation_read',
        'observations': [{
            'source_url': observation.public_canonical_url,
            'observation_id': observation.id,
            'observed_at': observation.observed_at.isoformat(),
            'price': observation.price,
            'currency': observation.currency,
            'fees_known': observation.fees_known,
        }],
    }
```

This illustrates the interface; `client` and `observation` belong to the consuming
application's adapter. This release does not install a live retailer probe.
`health_check` never submits collection jobs, purchases, sends messages or creates
accounts. Use separate authorized tools for such operations. Generic `execute` is
never substituted for a missing health probe. Exceptions become a redacted BROKEN
report; probe timeouts become UNAVAILABLE. Provider-specific status classification
is the adapter's responsibility.

Use CONFIGURATION_REQUIRED for missing approved API credentials, LOGIN_REQUIRED
for an expired/revoked login, BLOCKED for denied access, and RATE_LIMITED with
`retry_after_seconds` (60–86400) for a provider rate limit. Never label HTTP 200
alone as evidence of a usable catalogue; validate the domain payload.

If one tool handles several retailers/accounts, define a pure `health_scope(action)`
method returning a stable canonical source slug (1–100 letters/digits/`_.-`). It
must derive that slug from trusted connector configuration, **not arbitrary query
text, a random ID or credentials**. Related requests sharing a source/access limit
must share the slug. Without this method, all checks of that tool share `default`.
Cooldown state is keyed by app, owner, tool and this source. Other owners' reports
are private; an app-wide admission quota still applies across all owners/sources.

Evidence limits: at most ten observations, nonempty observation IDs, timezone-aware
ISO timestamps, finite nonnegative prices, ISO-style three-letter currency codes,
and public canonical HTTPS references without userinfo, query or fragment. The
platform never fetches those evidence URLs. Adapters must redact sensitive data,
including any private information in paths/IDs. Credentials belong in the private
adapter configuration, never task arguments, reports or source slugs.

## Register and execute a check

The operator grants both `runtime.connector_check` and the domain tool to the app.
No existing app grants are changed automatically. The app uses its normal bearer
credential and `X-External-User-ID`; it cannot supply a tenant/owner ID.

Create an agent through `POST /v1/task-agents`:

```json
{
  "idempotency_key": "catalogue-check-v1",
  "blueprint": {
    "name": "Check catalogue evidence",
    "description": "Verify an authorized source and flag missing evidence",
    "steps": [{
      "tool": "runtime.connector_check",
      "version": "1",
      "arguments": {
        "tool": "example.catalogue",
        "version": "1",
        "arguments": {"retailer": "configured-source"},
        "max_age_seconds": 3600,
        "require_observations": true,
        "require_price": true
      }
    }]
  }
}
```

`example.catalogue` is illustrative, not installed. Use the installed tool's actual
input schema and version. Invoke with the ordinary
`POST /v1/task-agents/{agent_id}/runs` and read `GET /v1/tasks/{task_id}`.
Required scopes are `agents:write`, `agents:run`, `tasks:write`, `tasks:read`;
`agents:read` is needed for normal agent/tool discovery.

## Schedule verification and notifications

Create `POST /v1/orchestration/automations` using the saved agent:

```json
{
  "idempotency_key": "catalogue-hourly-v1",
  "definition": {
    "agent_id": "REPLACE_WITH_SAVED_AGENT_UUID",
    "name": "Hourly catalogue verification",
    "trigger": {"type": "interval", "interval_seconds": 3600},
    "max_runs": 24,
    "condition": {"path": "result.needs_attention", "operator": "eq", "value": true},
    "notification": {"title": "Connector needs attention", "on": "crossing"}
  }
}
```

Approve the returned exact definition hash through the existing automation approval
endpoint (`tasks:approve`). This authorizes the bounded recurring READ checks;
there is no per-check approval. Pausing/archiving revokes queued execution.
For eight sources, save one agent/schedule per source so results and alerts remain
independent; all share the app quota and existing one-active-task-per-owner rule.
The scheduler's max-runs, daily automation budget and plan limits still apply.
DEFERRED checks also consume a scheduled occurrence but make no probe request.

`ConnectorCheckCompleted` is emitted transactionally with each report. Existing
signed event subscriptions can deliver it to the app. Its payload contains report
ID, tool, source, status, usable_evidence and needs_attention. Retrieve full evidence
through the authenticated report API. No WhatsApp/email send is enabled implicitly.

## APIs and recovery

- `GET /v1/connectors`: granted adapters and whether they implement health checks,
  this owner's cooldown states, and app check limits. Requires `tasks:read`.
- `GET /v1/connectors/reports?after=0&limit=50&tool=example.catalogue`: ordered,
  paginated evidence history with `next_after`. Requires `tasks:read`.
- `POST /v1/connectors/{tool}/{source}/resume`, body `{"report_id":"LATEST_UUID"}`:
  clear a block after addressing the underlying problem. Requires `tasks:write`.
  A stale report ID or an active check is rejected. Resume is audited, preserves
  cooldown/quotas and does not start a request or grant any new provider access.

Blocked/login/configuration states stop further automatic probes. Other failures
back off exponentially to one hour; explicit retry-after can extend to 24 hours.
Every successful check also has a 60-second minimum interval. Defaults are 60
admitted checks/hour/app and at most three concurrently. Operators set
`policy.connector_checks_per_hour` in the existing app manifest; zero disables
remote probes. Rejected/cooldown checks return execution=DEFERRED,
status=NOT_CHECKED and a reason; they never pretend to be fresh observations.

Each completed check stores evidence before worker completion. If the worker dies
in that gap, retry returns the same report with execution=CACHED, without another
probe. A crash during the remote read may leave a lease for up to 120 seconds;
checks defer until it expires. Adapter HTTP clients must also bound cancellation
and transport timeouts. Checking is limited to 20 seconds or the adapter's shorter
timeout. Quotas reserve attempts before the read, including failed reads.

These request limits are not a monetary billing guarantee. Provider/API pricing
and other model or collection usage remain separately governed.

## DealAgent boundary and verified coverage

DealAgent can add health probes to its existing retailer adapters and consume this
API without changing its collection/price-comparison ownership. Neither blocked
retailer access nor affiliate authorization is granted by this feature. Affiliate
credentials and approved provider access remain prerequisites for live API checks.

Tests use controlled adapter fixtures with real HTTP APIs, PostgreSQL migrations,
worker execution, scheduling, restart recovery, evidence validation, quotas,
source-level blocking, cross-app/owner isolation and non-bypass database RLS checks.
They do not certify the eight live retailers or perform a live affiliate request.

## Recovery suggestions and verification mode

Every report now includes `verification_mode`: `unspecified` by default,
`stored_observation` for a read of previously collected evidence, or `live_source`
only when the trusted adapter actually performs a fresh authorized source probe.
Adapters may include the same field in `ProbeResult`. Never infer a live source
check from a successful HTTP request to a local catalogue. Existing adapters remain
compatible and default to `unspecified`.

`recovery_options` carries bounded, plain-language actions for blocked access,
expired authorization, missing configuration, rate limits, broken adapters and
missing evidence. Deferred blocked checks repeat the last report's recovery
instructions without another provider request. No recovery suggestion automatically
changes a connector or transmits credentials.

To suggest an actual configured feed alternative, add `replaces` to an approved
`AI_FEED_PROFILES` entry for the **same app**:

```json
"replaces": [{"tool": "dealagent.product_observation", "source": "amazon"}]
```

This is a mapping example, not a claim that an Amazon feed is installed or approved.
The platform matches the exact tool and source; it never suggests another app's
profiles. Without a configured mapping, a blocked source gets instructions to
request authorized API/export access, not an invented provider integration.

## Remaining live retailer boundary (local review, 9 October 2026)

The inspected DealAgent `dealagent.product_observation` adapter now has a
`health_check` and retailer `health_scope`. It calls the engine's stored-observation
endpoint; it explicitly does **not** trigger retailer collection. Platform scheduling
can run that check, but it cannot prove that all eight retailers are currently live.
Fresh probes must be provided by the authorized DealAgent retailer adapters or
approved feeds, with real product URLs and any required location/account context.

DealAgent's own recorded 8 October checks describe these remaining sources:
Amazon CAPTCHA, Flipkart HTTP 500, Croma parser mismatch, JioMart route/location
mismatch, Blinkit robots restriction, BigBasket HTTP 403, Instamart and Zepto
location required. These are recorded findings, **not new live checks by this
platform change**. An authorized API/feed or corrected permitted adapter/input is
needed as appropriate. Do not bypass restrictions or label stored prices as newly
collected data. The platform changes do not modify DealAgent's repository.

### Live-probe follow-up

The optional platform bridge `dealagent.live_probe` now reuses DealAgent's HTTP
client, configured URLs and parsers for fresh bounded access/extraction checks.
The original eight sources (excluding Amazon and Flipkart) were executed locally
on 9 October. See [actual outcomes and task IDs](DEALAGENT_LIVE_CHECKS.md).
Stored-observation checks remain a separate tool. Refused and JavaScript-dependent
sources are reported as such; they are not labelled working catalogue coverage.

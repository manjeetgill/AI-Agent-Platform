# Reusing the platform across apps and business domains

The durable engine does not need to know a business domain. An app supplies its own trusted tools,
versioned built-in templates and policy. Users instantiate these templates into private reusable
agents or create custom plans using the same permitted tools. The app derives external-user IDs
from its own authenticated sessions. Credentials must stay on its backend.

## Onboard an app

1. Apply migrations (including `013_app_catalogues.sql`).
2. Provision an organisation/application with `python -m agent_platform.service_identity create`.
   Use `agents:read`, `agents:write`, `agents:run`, `tasks:read`, `tasks:write`; grant `tasks:approve`
   only to a backend that collects explicit human approval. Store the returned key privately.
3. Install any business tool package on the API and worker. Enable its exact entry-point name in
   `AI_PLATFORM_TOOL_PLUGINS` on both, then restart them. Packages are operator-reviewed Python
   code, not model-generated code and not a sandbox. Never install packages from page instructions.
4. Apply an app manifest with `python -m agent_platform.app_catalogue app.json`:

```json
{
  "app_id": "REPLACE_WITH_PROVISIONED_APP_UUID",
  "tools": ["shipping.estimate"],
  "policy": {
    "planning_calls_per_day": 500,
    "user_planning_calls_per_day": 50,
    "max_plan_steps": 5
  },
  "templates": [{
    "slug": "shipping-estimate",
    "version": 1,
    "blueprint": {
      "name": "Shipping estimate",
      "description": "Calculate from supplied rates; no booking",
      "steps": [{
        "tool": "shipping.estimate", "version": "1",
        "arguments": {"units": 3, "rate_minor_units": 125, "currency": "INR"}
      }]
    }
  }]
}
```

The sample values are illustrative; supply actual app/user inputs at instantiation. App-wide
built-ins must not contain an individual user's secrets or private data. Tool grants and policy
are replaced atomically by the manifest; templates are upserted by slug/version. Omitted templates
are preserved; set `enabled:false` on a specific version to disable it. Changed definitions require
a new version. Catalogue listing returns the newest version per slug; a disabled newest version
hides the slug rather than falling back. Disabled templates cannot be instantiated; existing saved
agents remain independent. Revoke a tool grant to stop future dispatch of that tool.

## API flow

Use `Authorization: Bearer <app-service-key>` and `X-External-User-ID: <authenticated-user>`:

- `GET /v1/task-agents/catalogue?limit=50&after=<slug>` — app built-ins, availability and policy.
- `POST /v1/task-agents/catalogue/{id}/instantiate` with `idempotency_key` and optional complete
  `arguments` array — create a private user agent without executing it.
- Existing `/interpret`, save, `/{id}/runs`, task status, events, stream and control endpoints work
  identically for every app. Each mutation needs a stable idempotency key where the schema supports it.

Template availability reflects installed adapter versions, current app grants and step limits.
Grants are rechecked at save, invocation and worker dispatch. Financial/SEND and USER_ONLY tool
restrictions cannot be downgraded by a template, model or manifest. READ tools execute without
approval prompts. Planning budgets count requests (including failed attempts), not money or tokens.
Step limits apply at admission; an already running task keeps its saved plan. A policy cannot exceed
an endpoint's existing schema maximum (currently ten steps for reusable task agents).

## Business-owned adapters

`examples/shipping_plugin` is a runnable separate package using the `agent_platform.tools` Python
entry-point group. Its zero-argument factory returns `{tool_name: adapter}`. Explicitly enable
`shipping-example` after installing it. No core registry edit is required. Missing/duplicate packages,
name collisions and invalid contracts fail startup. API and worker must install matching versions.
App grants remain required even after a plugin is installed. The example calculates a supplied-rate
estimate only; it does not contact a carrier or spend money.

## Verified boundaries and remaining work

Tests cover separate app catalogues and same-external-ID isolation, template ownership, idempotent
instantiation, immutable versions, transaction rollback, revoked tool availability, app quota/step
limits, explicit package selection and a business plugin run through the worker.

This provides reusable extension points, not universal automatic integration. Arbitrary remote/MCP
tool registration, secret vaults, dependent/branching steps, durable timers, multiple simultaneous
active chores per user, complete budgets/billing, OAuth and self-service app administration remain
separate work. Existing NRAIAlgo evidence adapters still belong to that app; other apps should use
the generic task API with their own adapters. No household features were added.

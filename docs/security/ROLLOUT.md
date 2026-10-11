# Security rollout

## Implemented versus operational

| Area | Implemented | Operational requirement |
| --- | --- | --- |
| Isolation | All classified tenant/app tables have forced RLS; worker bypass requires role membership and worker context. Request scope is transaction-local. Public callbacks establish narrow identity and still validate signatures. | Provision roles, back up DB, run migrations 050–054, restart with distinct credentials. |
| Delegation | Ed25519 issuer keyring, app/tenant binding, key validity windows, 60-second maximum lifetime, single-use PostgreSQL replay ledger and bounded expiry pruning. Existing approvals unchanged. | Install issuer private key in consuming app, public key on platform, verify fresh tokens, then set legacy retirement cutoff. |
| Gateway | Ordered explicit providers, atomic per-app and per-provider daily reservations, persisted cooldowns, sensitivity allowlist, estimated costs and UTC reset. Unknown outcomes retain their reservation. | Explicit provider prices/keys and each app's policy. No paid provider is enabled automatically. |
| Workbook | Real migration-039 aggregate export and a replacement workbook. Missing tokens/costs are marked unknown. | Refresh export from the deployment being reported; provider invoices remain authoritative. |
| Prototype | No identified prototype deleted. | Supply accessible exact path before archival and provider-key rotation. |

## Database roles and upgrade

Use a dedicated platform database. Take a private `pg_dump -Fc` backup first.
Stop platform application processes during ownership/migration cutover; preserve database volumes.

1. Supply the existing administrator connection privately as `AGENT_DB_ADMIN_URL`.
2. Run `python scripts/database_roles.py --output /private/path/database-roles.json`.
   The script refuses unknown tables, pre-existing login roles and output-file overwrite.
   It uses one transaction, creates restricted migration/API/worker roles, changes
   table ownership to the migrator, and saves generated credentials with mode 0600.
   On failure, inspect the error; do not delete an existing valid credential file.
3. `python scripts/runtime_env.py --config .env --roles /private/path/database-roles.json`.
   It writes separate migration/API/worker env files. Common settings exclude all
   known administrator, operator and alternate-role database credentials.
4. Run the migration service, then API and worker services using `compose.yaml`.
   The deployment release helper requires `/opt/agent-platform/secrets/database-roles.json`
   (override with `AGENT_DATABASE_ROLES_FILE`) and separates credentials before restart.
5. Check readiness, an authenticated read, a durable task, webhook routing and worker
   heartbeats. Runtime startup rejects superuser, BYPASSRLS, CREATEROLE and CREATEDB.

The migrator owns schema objects. The API cannot assume the worker role. Workers
have cross-tenant access for queue processing, so remain trusted backend processes.
The local operator console additionally requires an explicit private
`AI_PLATFORM_OPERATOR_DATABASE_URL`; never supply it to the public production API.

For local setup: `python scripts/local.py roles`, then `python scripts/local.py start`.
For the separate workspace database: `python scripts/local.py roles --console`, then
`python scripts/console.py`. Console and API registries are separate from the worker.
Historical migrations 001–049 are unchanged. The rollout does not silently migrate
or restart an existing deployment simply because source files have changed.

## Mandatory regression gate

With a disposable PostgreSQL cluster and its admin URL in `AGENT_TEST_ADMIN_URL`:

```sh
pip install -e '.[dev]'
python scripts/test_restricted.py tests/python
```

The runner creates a uniquely named DB and restricted API/worker/migration users,
runs migrations, tests, then drops only those resources. CI runs the same command
for dev and dev,voice installations. No live provider is contacted by these tests.
All tenant tables are enumerated for fail-closed checks, including forged worker
flags and mismatched app/tenant scopes. API tests use the API credential; direct
worker fixtures use the worker credential. This does not simulate a hostile worker
or prove every possible application-level authorization path.

## Delegation migration and rotation

Generate issuer material:

```sh
python scripts/delegation_keys.py --issuer nraialgo --tenant nraialgo \
  --app-id REAL_EXISTING_APP_UUID --directory /private/new-key-directory
```

Only `issuer-private.json` belongs on the consuming app's server. Merge the public
entry into `AI_DELEGATION_ISSUER_KEYS` on the platform, retaining current keys during
rotation. Each public entry binds issuer, kid, tenant, app ID and validity period.
For the existing NRAIAlgo research app only, add `--legacy-research-access`; this
operator grant does not extend to ordinary service keys or other issuer keys.
Use `agent_platform.delegation.issue_from_file` or `agent_platform.delegation.issue` (or the same documented wire format) to sign a
fresh token for **each HTTP request**. Do not reuse a token across polling requests.
Verify an authenticated read, wrong-app rejection and replay rejection before
switching all requests. Then set `AI_DELEGATION_LEGACY_UNTIL` to an explicit Unix
cutoff. Leaving it blank preserves compatibility; it does not complete retirement.
After the overlap and token lifetime, remove the previous public key. Never send
private issuer keys to the platform or include them in browser code.

This migration is for app-to-platform delegation. The separate legacy
platform-to-NRAIAlgo evidence connector supports `AI_CONNECTOR_ISSUER_KEY_FILE`
for a platform-owned Ed25519 issuer and `AI_CONNECTOR_LEGACY_UNTIL` for its old format.
Install verification/replay support in the NRAIAlgo receiver before enabling that key.
Do not retire or rotate the shared secret until both directions have switched.

## Provider budgets

`AI_MODEL_PROVIDER_CHAIN` is an ordered JSON array with explicit provider, model,
key_env, daily_budget_microusd, microusd_per_token, sensitivity and cooldown_seconds.
USD amounts are millionths. Supply verified provider rates; the per-token rate
must conservatively cover the larger of input/output charges, including reasoning
where applicable. No production rate is guessed by this implementation.

Create an app policy using `scripts/model_policy.py --app-id UUID
--daily-microusd LIMIT --daily-calls LIMIT --provider PROVIDER --sensitivity public`.
Run with the private worker DB environment. `--report` groups actual attempts,
tokens, estimated cost and unresolved reservations by provider/model/status.
A missing app policy, provider key or budget storage blocks routing. Disallowed
sensitivity/provider combinations are skipped. Only explicit 429/503 backpressure
can advance to the next provider; timeouts and ambiguous outcomes do not retry.
Daily quotas use UTC dates. Cooldowns persist across midnight. Reservations are
not invoices; unknown usage remains reserved and must not be reset blindly.

## Usage workbook

`python scripts/export_model_usage.py --output /private/new-usage.json` exports
aggregate real records from `platform_model_calls` (migration 039), with no prompts,
user identifiers or credentials. Use the restricted worker credential for all-app
reporting. Missing cost is unknown, never zero; currency is not recorded by this
legacy table. Provider-chain USD estimates are reported separately until a common
billing currency/source is recorded. Email, speech and carrier invoices are excluded.


## Verified local cutover — 9 October 2026

Both existing local platform databases now have migrations through 054. Private
backups and rollout receipts are under `.runtime/local/security-backup-*`.
The existing macOS `com.dealagent.platform` launch agent now uses this canonical
checkout. Its virtual environment and original PostgreSQL data directory were
preserved. API connections use `agent_api`; background connections use `agent_worker`.
The six global/operator classifications include the already-protected worker
heartbeat table; all 103 tenant/app tables are forced-RLS, with 104 forced-RLS
public tables overall. No-scope checks passed on all 103 against the live local DB.

A synthetic comparison completed through the actual HTTP API and task worker:
`6f9544c4-2d91-4e4b-837c-c321a8da5357`. A second app received 404 for that task.
Both temporary test credentials were revoked. No model, messaging or calling
provider was invoked. Health/readiness returned 200, OpenAPI 404, anonymous tasks 401.

The provider-wide cap now sums reservations across all apps under a transaction
advisory lock; it cannot multiply by the number of subscribed apps. Migration 054
returns only aggregate counters to the API. Cooldowns likewise apply provider-wide.

NRAIAlgo companion code is committed locally as `f7ddf4ca`, including migration 116,
Ed25519 signing/verification, key rotation overlap and inbound replay rejection.
Nine consumer tests and a two-way cryptographic/PostgreSQL concurrency check passed.
Deploy that consumer migration/code and install both issuer keypairs before setting
retirement cutoffs. Its unrelated in-progress changes were preserved; it was not
restarted or deployed by this rollout. The DigitalOcean deployment was not changed.

The replacement usage workbook was generated from real local migration-039 records:
38 calls, 369,148 known tokens, and 38 calls with unknown cost. The original workbook
and prototype paths have not been supplied. macOS denies Desktop access; neither
an unknown prototype nor its environment file has been archived or modified.

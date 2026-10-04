# Agent integration protocol v1

Two independently deployed projects communicate by HTTP JSON. Neither imports the other's code or accesses its database. NRAIAlgo owns the browser UI, `/v1/agents/*` facade, session/MFA/edition checks and evidence endpoints. The platform owns `/v1/agents/*` service APIs, model calls, agent storage and migrations.

Delegation wire format is `base64url(canonical JSON).base64url(HMAC-SHA256(key, encoded_body))`, without base64 padding. Canonical JSON uses sorted keys and separators `,` and `:`. Claims are `iss`, `aud`, `tenant`, `user`, `scope`, `agent`, `run`, `iat`, `exp`, `jti`; agent/run are nullable for browser delegation. IDs are strings. Timestamps are Unix seconds. Default lifetime is 45 seconds, maximum 60, future clock tolerance 5 seconds. Secrets must be at least 32 characters.

Browser facade delegation: issuer `nraialgo`, audience `agent-platform`, scope `agents:access`. Tool delegation: issuer `agent-platform`, audience `nraialgo-connector`, tenant `nraialgo`, scope `<tool>:read`, with user/agent/run IDs. The connector checks current user access and ownership independently.

Connector POST endpoints:

| Path | Body | Scope |
| --- | --- | --- |
| `/v1/agent-connector/scanner-evidence` | `run_id`, `instrument` | `scanner_evidence:read` |
| `/v1/agent-connector/backtest-evidence` | `backtest_id` | `backtest_evidence:read` |
| `/v1/agent-connector/paper-trade-evidence` | `trade_id` | `paper_trade_evidence:read` |

Scanner evidence returns source/session/timeframe/events/indicators/freshness. Other evidence returns source/asOf/facts/freshness. Empty or inaccessible records are 404; rejected delegation is 401; revoked user or feature permission is 403. Model output cannot grant a scope. No broker or execution endpoint is exposed.

Both repositories own small token adapters and an identical fixed protocol fixture (`tests/fixtures/agent_delegation_v1.json`). Run the contract tests in both projects when changing this boundary. Breaking changes require an explicit protocol version and coordinated release; copying internal runtime code into a consumer is not part of the contract.

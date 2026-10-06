-- App-owned telephone gateways. Carrier credentials remain on the app's gateway.
CREATE TABLE telephony_connections (
 id uuid PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL,
 external_id text NOT NULL,
 agent_id uuid NOT NULL REFERENCES web_voice_agents(id) ON DELETE CASCADE,
 number text NOT NULL CHECK(number ~ '^\+[1-9][0-9]{7,14}$'),
 gateway_hash text NOT NULL,
 enabled boolean NOT NULL DEFAULT false,
 carrier_verified_at timestamptz,
 verification_reference text,
 heartbeat_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,external_id)
);
-- A verified number cannot silently route to a second app or customer.
CREATE UNIQUE INDEX telephony_verified_number ON telephony_connections(number) WHERE carrier_verified_at IS NOT NULL;
CREATE INDEX telephony_app_connections ON telephony_connections(app_id,owner_id,created_at,id);
CREATE TABLE telephony_app_limits (
 app_id uuid PRIMARY KEY REFERENCES platform_apps(id) ON DELETE CASCADE,
 concurrent_calls integer NOT NULL DEFAULT 2 CHECK(concurrent_calls BETWEEN 1 AND 100),
 daily_calls integer NOT NULL DEFAULT 100 CHECK(daily_calls BETWEEN 1 AND 100000)
);
CREATE TABLE telephony_sessions (
 id uuid PRIMARY KEY,
 connection_id uuid NOT NULL REFERENCES telephony_connections(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 call_id uuid NOT NULL,
 state text NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','ENDED','FAILED','UNKNOWN')),
 turns integer NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '120 seconds',
 ended_at timestamptz,
 UNIQUE(connection_id,call_id)
);
CREATE INDEX telephony_app_usage ON telephony_sessions(app_id,created_at);
CREATE INDEX telephony_active ON telephony_sessions(app_id,state,expires_at);
CREATE TABLE telephony_turns (
 session_id uuid NOT NULL REFERENCES telephony_sessions(id) ON DELETE CASCADE,
 turn integer NOT NULL CHECK(turn BETWEEN 1 AND 15),
 input_hash text NOT NULL,
 state text NOT NULL CHECK(state IN ('STARTED','DONE','UNKNOWN')),
 input_text text NOT NULL,
 reply jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(session_id,turn)
);
CREATE TABLE telephony_audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 connection_id uuid NOT NULL REFERENCES telephony_connections(id) ON DELETE CASCADE,
 event text NOT NULL,
 actor text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write',
 'telephony:read','telephony:write']::text[]);

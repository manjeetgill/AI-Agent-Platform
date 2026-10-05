CREATE TABLE calling_agents (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, external_id text NOT NULL, name text NOT NULL,
 connection_ref text, language text NOT NULL, greeting text NOT NULL,
 handoff_message text NOT NULL, max_turns integer NOT NULL CHECK(max_turns BETWEEN 1 AND 30),
 enabled boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,external_id)
);
CREATE UNIQUE INDEX calling_connection_binding ON calling_agents(connection_ref) WHERE connection_ref IS NOT NULL;
CREATE TABLE calling_calls (
 id uuid PRIMARY KEY, agent_id uuid NOT NULL REFERENCES calling_agents(id) ON DELETE CASCADE,
 provider_call_id text NOT NULL, caller text NOT NULL,
 state text NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','HANDOFF','COMPLETED','FAILED')),
 last_turn integer NOT NULL DEFAULT 0, duration_seconds integer, status_sequence integer NOT NULL DEFAULT -1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(agent_id,provider_call_id)
);
CREATE INDEX calling_history ON calling_calls(agent_id,created_at,id);
CREATE TABLE calling_turns (
 id uuid PRIMARY KEY, call_id uuid NOT NULL REFERENCES calling_calls(id) ON DELETE CASCADE,
 turn integer NOT NULL, request_hash text NOT NULL,
 state text NOT NULL CHECK(state IN ('PROCESSING','DONE','UNKNOWN')),
 response_xml text, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(call_id,turn)
);
CREATE TABLE calling_audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 agent_id uuid NOT NULL REFERENCES calling_agents(id) ON DELETE CASCADE,
 event text NOT NULL, actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);

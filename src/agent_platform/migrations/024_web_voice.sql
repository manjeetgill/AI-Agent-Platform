CREATE TABLE web_voice_agents (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, external_id text NOT NULL, name text NOT NULL,
 greeting text NOT NULL, fallback text NOT NULL, documents uuid[] NOT NULL DEFAULT '{}',
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','app')),
 enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,external_id)
);
CREATE TABLE web_voice_sessions (
 id uuid PRIMARY KEY, agent_id uuid NOT NULL REFERENCES web_voice_agents(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE, owner_id uuid NOT NULL,
 request_key text NOT NULL, offer_hash text NOT NULL, runtime_id uuid NOT NULL,
 state text NOT NULL CHECK(state IN ('STARTING','ACTIVE','ENDED','FAILED','UNKNOWN')),
 turns integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '2 minutes', ended_at timestamptz,
 UNIQUE(app_id,owner_id,request_key)
);
CREATE INDEX web_voice_active ON web_voice_sessions(state,expires_at);
CREATE TABLE web_voice_turns (
 id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES web_voice_sessions(id) ON DELETE CASCADE,
 turn integer NOT NULL, input_hash text NOT NULL,
 state text NOT NULL CHECK(state IN ('STARTED','DONE','UNKNOWN')), reply jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(session_id,turn)
);

CREATE TABLE telephony_tool_configs (
 connection_id uuid PRIMARY KEY REFERENCES telephony_connections(id) ON DELETE CASCADE,
 revision uuid NOT NULL,
 endpoint text NOT NULL,
 secret_cipher text NOT NULL,
 tools jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE telephony_call_authorizations (
 session_id uuid PRIMARY KEY REFERENCES telephony_sessions(id) ON DELETE CASCADE,
 subject text NOT NULL,
 tools text[] NOT NULL,
 revision uuid NOT NULL,
 authorized_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE telephony_tool_actions (
 id uuid PRIMARY KEY,
 session_id uuid NOT NULL REFERENCES telephony_sessions(id) ON DELETE CASCADE,
 turn integer NOT NULL,
 config_revision uuid NOT NULL,
 tool text NOT NULL,
 risk text NOT NULL CHECK(risk IN ('READ','WRITE','USER_ONLY')),
 arguments jsonb NOT NULL,
 summary text NOT NULL,
 action_hash text NOT NULL,
 subject text,
 authorization_revision uuid,
 state text NOT NULL CHECK(state IN ('PENDING','SENDING','DONE','UNKNOWN','CANCELLED','NOT_APPLIED')),
 approved_turn integer,
 result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(session_id,turn)
);
CREATE INDEX telephony_pending_actions ON telephony_tool_actions(session_id,state);
CREATE TABLE telephony_model_usage (
 session_id uuid NOT NULL REFERENCES telephony_sessions(id) ON DELETE CASCADE,
 turn integer NOT NULL,
 state text NOT NULL CHECK(state IN ('STARTED','DONE','UNKNOWN')),
 tokens integer NOT NULL DEFAULT 0,
 provider text, model text,
 PRIMARY KEY(session_id,turn)
);
CREATE TABLE telephony_speech_usage (
 session_id uuid NOT NULL REFERENCES telephony_sessions(id) ON DELETE CASCADE,
 turn integer NOT NULL CHECK(turn BETWEEN 0 AND 15),
 input_hash text NOT NULL,
 state text NOT NULL CHECK(state IN ('STARTED','DONE','UNKNOWN')),
 input_seconds numeric NOT NULL DEFAULT 0,
 output_seconds numeric NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(session_id,turn)
);

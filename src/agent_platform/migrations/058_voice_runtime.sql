-- Voice additions follow the production security and SMS migrations through 057.
ALTER TABLE web_voice_agents ADD COLUMN config jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE web_voice_agents ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK(version > 0);
CREATE TABLE web_voice_versions (
    agent_id uuid NOT NULL REFERENCES web_voice_agents(id) ON DELETE CASCADE,
    version integer NOT NULL CHECK(version > 0),
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(agent_id,version)
);
INSERT INTO web_voice_versions(agent_id,version,snapshot)
SELECT id,version,to_jsonb(a) FROM web_voice_agents a;
ALTER TABLE web_voice_sessions ADD COLUMN agent_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE web_voice_sessions ADD COLUMN transcript_consent boolean NOT NULL DEFAULT false;
ALTER TABLE web_voice_sessions ADD COLUMN event_sequence integer NOT NULL DEFAULT 0;
CREATE TABLE web_voice_events (
    session_id uuid NOT NULL REFERENCES web_voice_sessions(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    kind text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(session_id,sequence)
);
CREATE INDEX web_voice_sessions_owner_history ON web_voice_sessions(app_id,owner_id,created_at DESC);
CREATE TABLE web_voice_transcripts (
    session_id uuid NOT NULL REFERENCES web_voice_sessions(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    role text NOT NULL CHECK(role IN ('user','assistant')),
    content text NOT NULL CHECK(length(content)<=1500),
    expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
    PRIMARY KEY(session_id,sequence)
);
CREATE INDEX web_voice_transcripts_expiry ON web_voice_transcripts(expires_at);

-- Provider-neutral memory. No existing key gains new access automatically.
ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write',
 'telephony:read','telephony:write','memory:read','memory:write','memory:review','alerts:send']::text[]);
ALTER TABLE agent_memory ADD COLUMN validation_status text NOT NULL DEFAULT 'UNVERIFIED'
 CHECK(validation_status IN ('USER_SUPPLIED','UNVERIFIED'));
UPDATE agent_memory SET validation_status='USER_SUPPLIED' WHERE kind='PREFERENCE';
CREATE TABLE platform_memories (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, request_key text NOT NULL, fingerprint text NOT NULL,
 namespace text NOT NULL DEFAULT 'general', kind text NOT NULL CHECK(kind IN ('FACT','PREFERENCE','PATTERN','DECISION')),
 content text NOT NULL CHECK(length(content) BETWEEN 1 AND 600),
 source_type text NOT NULL CHECK(source_type IN ('USER','TOOL','TRUSTED_SOURCE','MODEL')),
 source_ref text NOT NULL, confidence double precision NOT NULL CHECK(confidence>=0 AND confidence<=1),
 visibility text NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','app')),
 status text NOT NULL DEFAULT 'CANDIDATE' CHECK(status IN ('CANDIDATE','VALIDATED','REJECTED','REVOKED')),
 version integer NOT NULL DEFAULT 1, expires_at timestamptz NOT NULL,
 reviewer text, validation_method text, validation_ref text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,request_key)
);
CREATE INDEX platform_memories_scope ON platform_memories(app_id,owner_id,namespace,status,expires_at);
CREATE INDEX platform_memories_text ON platform_memories USING gin(to_tsvector('simple',content));
CREATE TABLE platform_memory_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, memory_id uuid, event text NOT NULL, actor text NOT NULL,
 details jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX platform_memory_events_scope ON platform_memory_events(app_id,owner_id,id);
CREATE TABLE task_checkpoints (
 task_id uuid PRIMARY KEY REFERENCES chore_tasks(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, revision integer NOT NULL, task_revision integer NOT NULL,
 payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent_outcome_feedback (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, request_key text NOT NULL, fingerprint text NOT NULL,
 target_type text NOT NULL CHECK(target_type IN ('task','answer')), target_id uuid NOT NULL,
 outcome text NOT NULL CHECK(outcome IN ('CONFIRMED_SUCCESS','CORRECTION','FAILED','UNKNOWN')),
 evidence_ref text NOT NULL, note text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(app_id,owner_id,request_key)
);
CREATE TABLE task_handoff_snapshots (
 task_id uuid PRIMARY KEY REFERENCES chore_tasks(id) ON DELETE CASCADE,
 app_id uuid NOT NULL REFERENCES platform_apps(id) ON DELETE CASCADE,
 owner_id uuid NOT NULL, payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_memory (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('PREFERENCE','OBSERVATION')),
 content text NOT NULL CHECK(length(content) BETWEEN 1 AND 600),
 source_run uuid REFERENCES agent_runs(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK ((kind='PREFERENCE' AND source_run IS NULL) OR (kind='OBSERVATION' AND source_run IS NOT NULL))
);
CREATE INDEX agent_memory_owner ON agent_memory(tenant_id,owner_id,created_at DESC);
CREATE INDEX agent_memory_text ON agent_memory USING gin(to_tsvector('english',content));

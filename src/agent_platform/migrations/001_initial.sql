CREATE TABLE agent_definitions (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id uuid NOT NULL,
 name text NOT NULL, description text NOT NULL DEFAULT '', kind text NOT NULL DEFAULT 'custom'
 CHECK (kind IN ('custom','builtin')), status text NOT NULL DEFAULT 'active'
 CHECK (status IN ('active','disabled')), current_version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_owner_list ON agent_definitions(tenant_id,owner_id,updated_at DESC);
CREATE TABLE agent_versions (
 agent_id uuid NOT NULL REFERENCES agent_definitions(id) ON DELETE CASCADE,
 version integer NOT NULL CHECK (version>0), instructions text NOT NULL,
 allowed_tools jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(agent_id,version)
);
CREATE TABLE agent_runs (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id uuid NOT NULL,
 agent_id uuid NOT NULL, agent_version integer NOT NULL,
 trigger text NOT NULL DEFAULT 'on_demand', idempotency_key text NOT NULL,
 status text NOT NULL CHECK (status IN ('CREATED','QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','TIMEOUT')),
 input jsonb NOT NULL, report jsonb, evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
 error_code text, provider text, model text, tokens integer NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz,
 FOREIGN KEY(agent_id,agent_version) REFERENCES agent_versions(agent_id,version),
 UNIQUE(tenant_id,owner_id,idempotency_key)
);
CREATE INDEX agent_run_owner_list ON agent_runs(tenant_id,owner_id,created_at DESC);
CREATE UNIQUE INDEX agent_one_active_run ON agent_runs(agent_id) WHERE status IN ('CREATED','QUEUED','RUNNING');

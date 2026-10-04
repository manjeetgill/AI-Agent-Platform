CREATE TABLE agent_workflow_runs (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('signal_review','backtest_review')),
 idempotency_key text NOT NULL, status text NOT NULL
 CHECK(status IN ('CREATED','RUNNING','COMPLETED','FAILED','TIMEOUT')),
 step_run_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, lease_until timestamptz,
 UNIQUE(tenant_id,owner_id,idempotency_key)
);
CREATE UNIQUE INDEX agent_one_active_workflow ON agent_workflow_runs(tenant_id,owner_id)
 WHERE status IN ('CREATED','RUNNING');
CREATE INDEX agent_workflow_history ON agent_workflow_runs(tenant_id,owner_id,created_at DESC);

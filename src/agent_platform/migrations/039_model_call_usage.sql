CREATE TABLE platform_model_calls (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 operation text NOT NULL, status text NOT NULL CHECK(status IN ('COMPLETED','FAILED')),
 provider text, model text, total_tokens integer CHECK(total_tokens>=0),
 latency_ms integer NOT NULL CHECK(latency_ms>=0), estimated_cost numeric CHECK(estimated_cost>=0),
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE INDEX platform_model_usage ON platform_model_calls(app_id,owner_id,created_at);
ALTER TABLE platform_model_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_model_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY model_call_scope ON platform_model_calls USING (
 current_setting('agent.worker',true)='on' OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
 AND owner_id::text=current_setting('agent.owner',true))) WITH CHECK (
 current_setting('agent.worker',true)='on' OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
 AND owner_id::text=current_setting('agent.owner',true)));

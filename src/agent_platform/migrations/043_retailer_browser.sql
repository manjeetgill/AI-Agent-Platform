CREATE TABLE retailer_browser_devices (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 key_id uuid NOT NULL REFERENCES platform_service_keys(id), secret_hash text NOT NULL,
 retailers text[] NOT NULL, expires_at timestamptz NOT NULL DEFAULT now()+interval '1 day',
 revoked_at timestamptz, last_seen_at timestamptz,
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE TABLE retailer_browser_jobs (
 id uuid PRIMARY KEY, operation_id text NOT NULL UNIQUE, tenant_id text NOT NULL,
 app_id uuid NOT NULL, owner_id uuid NOT NULL, task_id uuid NOT NULL,
 step_position integer NOT NULL, source text NOT NULL, action_hash text NOT NULL,
 arguments jsonb NOT NULL, state text NOT NULL DEFAULT 'PENDING'
 CHECK(state IN ('PENDING','STARTED','DONE')),
 device_id uuid REFERENCES retailer_browser_devices(id), result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz,
 expires_at timestamptz NOT NULL DEFAULT now()+interval '5 minutes',
 FOREIGN KEY(task_id,tenant_id,app_id,owner_id) REFERENCES chore_tasks(id,tenant_id,source_app_id,owner_id),
 CHECK(result IS NULL OR octet_length(result::text)<=12000)
);
CREATE INDEX retailer_browser_pending ON retailer_browser_jobs(app_id,owner_id,created_at) WHERE state='PENDING';
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['retailer_browser_devices','retailer_browser_jobs'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY browser_scope ON %I USING (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))
   WITH CHECK (current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))$p$,t);
 END LOOP;
END $$;

-- Generic connector checks reuse durable task execution and scheduling.
CREATE TABLE connector_check_state (
 tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 tool text NOT NULL CHECK(length(tool) BETWEEN 1 AND 100),
 source text NOT NULL DEFAULT 'default' CHECK(source ~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$'),
 failures integer NOT NULL DEFAULT 0 CHECK(failures>=0),
 needs_attention boolean NOT NULL DEFAULT false,
 next_check_at timestamptz NOT NULL DEFAULT now(),
 lease_token uuid, lease_until timestamptz,
 PRIMARY KEY(app_id,owner_id,tool,source),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE TABLE connector_check_reports (
 id uuid PRIMARY KEY, sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 task_id uuid NOT NULL, step_position integer NOT NULL,
 tool text NOT NULL, source text NOT NULL DEFAULT 'default', tool_version text NOT NULL, action_hash text NOT NULL,
 result jsonb NOT NULL CHECK(octet_length(result::text)<=16000),
 checked_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(task_id,step_position),
 FOREIGN KEY(task_id,tenant_id,app_id,owner_id) REFERENCES chore_tasks(id,tenant_id,source_app_id,owner_id),
 FOREIGN KEY(app_id,owner_id,tool,source) REFERENCES connector_check_state(app_id,owner_id,tool,source)
);
CREATE INDEX connector_report_history ON connector_check_reports(app_id,owner_id,sequence);
CREATE TABLE connector_check_usage (
 app_id uuid NOT NULL REFERENCES platform_apps(id), hour timestamptz NOT NULL,
 checks integer NOT NULL DEFAULT 0 CHECK(checks>=0), PRIMARY KEY(app_id,hour)
);
ALTER TABLE platform_app_policies ADD COLUMN connector_checks_per_hour integer NOT NULL DEFAULT 60
 CHECK(connector_checks_per_hour BETWEEN 0 AND 10000);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['connector_check_state','connector_check_reports'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY connector_scope ON %I USING (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true)
    AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))
   WITH CHECK (current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true)
    AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))$p$,t);
 END LOOP;
END $$;

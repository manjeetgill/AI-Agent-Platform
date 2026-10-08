-- Generic orchestration: domain observations remain in consuming applications.
ALTER TABLE task_agents ADD COLUMN status text NOT NULL DEFAULT 'ACTIVE'
 CHECK(status IN ('DRAFT','ACTIVE','PAUSED','DISABLED','ARCHIVED'));
ALTER TABLE task_agents ADD COLUMN manifest jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE task_agents ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE platform_apps ADD CONSTRAINT platform_app_tenant UNIQUE(id,tenant_id);
ALTER TABLE platform_external_users ADD CONSTRAINT platform_app_owner UNIQUE(app_id,owner_id);
ALTER TABLE task_agents ADD CONSTRAINT task_agent_identity UNIQUE(id,tenant_id,app_id,owner_id);
ALTER TABLE task_agents ADD CONSTRAINT task_agent_app_tenant FOREIGN KEY(app_id,tenant_id)
 REFERENCES platform_apps(id,tenant_id);
ALTER TABLE chore_tasks ADD CONSTRAINT chore_task_identity UNIQUE(id,tenant_id,source_app_id,owner_id);

CREATE TABLE automation_preferences (
 tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 namespace text NOT NULL CHECK(length(namespace) BETWEEN 1 AND 100),
 value jsonb NOT NULL CHECK(octet_length(value::text)<=8000),
 version integer NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(app_id,owner_id,namespace),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE TABLE automation_events (
 id uuid PRIMARY KEY, sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 event_type text NOT NULL CHECK(length(event_type) BETWEEN 1 AND 100), version integer NOT NULL CHECK(version>0),
 source text NOT NULL, correlation_id text NOT NULL, request_key text NOT NULL,
 payload jsonb NOT NULL CHECK(octet_length(payload::text)<=16000),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,source,request_key), UNIQUE(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE INDEX automation_event_feed ON automation_events(app_id,owner_id,sequence);
CREATE TABLE automations (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 agent_id uuid NOT NULL, definition jsonb NOT NULL, definition_hash text NOT NULL,
 request_key text NOT NULL, principal text NOT NULL,
 status text NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','ACTIVE','PAUSED','DISABLED','ARCHIVED')),
 next_run_at timestamptz NOT NULL, last_event_sequence bigint NOT NULL DEFAULT 0,
 run_count integer NOT NULL DEFAULT 0, last_matched boolean NOT NULL DEFAULT false,
 approved_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,request_key), UNIQUE(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(agent_id,tenant_id,app_id,owner_id) REFERENCES task_agents(id,tenant_id,app_id,owner_id)
);
CREATE INDEX automation_due ON automations(next_run_at,id) WHERE status='ACTIVE';
CREATE TABLE automation_runs (
 id uuid PRIMARY KEY, automation_id uuid NOT NULL, tenant_id text NOT NULL,
 app_id uuid NOT NULL, owner_id uuid NOT NULL,
 task_id uuid NOT NULL UNIQUE REFERENCES chore_tasks(id), occurrence text NOT NULL,
 evaluated_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(automation_id,occurrence),
 FOREIGN KEY(automation_id,tenant_id,app_id,owner_id) REFERENCES automations(id,tenant_id,app_id,owner_id)
 ,FOREIGN KEY(task_id,tenant_id,app_id,owner_id) REFERENCES chore_tasks(id,tenant_id,source_app_id,owner_id)
);
CREATE INDEX automation_pending_results ON automation_runs(created_at) WHERE evaluated_at IS NULL;
CREATE TABLE automation_notifications (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 event_id uuid NOT NULL REFERENCES automation_events(id),
 channel text NOT NULL DEFAULT 'in_app' CHECK(channel='in_app'),
 title text NOT NULL CHECK(length(title) BETWEEN 1 AND 160),
 body text NOT NULL CHECK(length(body)<=2000), evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), read_at timestamptz,
 UNIQUE(event_id,channel),
 FOREIGN KEY(event_id,tenant_id,app_id,owner_id) REFERENCES automation_events(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE INDEX automation_inbox ON automation_notifications(app_id,owner_id,created_at DESC,id);
CREATE TABLE automation_usage (
 app_id uuid NOT NULL REFERENCES platform_apps(id), day date NOT NULL DEFAULT CURRENT_DATE,
 runs integer NOT NULL DEFAULT 0 CHECK(runs>=0), PRIMARY KEY(app_id,day)
);
ALTER TABLE platform_app_policies ADD COLUMN automation_runs_per_day integer NOT NULL DEFAULT 100
 CHECK(automation_runs_per_day BETWEEN 0 AND 10000);

-- Application transactions set all three scope values. Worker discovery uses an
-- explicit transaction-local worker context. Deployment credentials must not be
-- superusers or BYPASSRLS. Never share these SQL credentials with consuming apps.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['automation_preferences','automation_events','automations',
                          'automation_runs','automation_notifications'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY automation_scope ON %I USING (
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

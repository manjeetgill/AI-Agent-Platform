CREATE TABLE automation_webhooks (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 destination text NOT NULL, event_type text NOT NULL, event_version integer NOT NULL DEFAULT 1,
 enabled boolean NOT NULL DEFAULT true, last_sequence bigint NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,destination,event_type,event_version), UNIQUE(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE TABLE automation_deliveries (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 webhook_id uuid NOT NULL, event_id uuid NOT NULL, attempts integer NOT NULL DEFAULT 0,
 status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','RUNNING','DELIVERED','DEAD','CANCELLED')),
 next_at timestamptz NOT NULL DEFAULT now(), lease_token uuid, lease_until timestamptz,
 delivered_at timestamptz, http_status integer,
 UNIQUE(webhook_id,event_id),
 FOREIGN KEY(webhook_id,tenant_id,app_id,owner_id) REFERENCES automation_webhooks(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(event_id,tenant_id,app_id,owner_id) REFERENCES automation_events(id,tenant_id,app_id,owner_id)
);
CREATE INDEX automation_delivery_due ON automation_deliveries(next_at) WHERE status IN ('QUEUED','RUNNING');
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['automation_webhooks','automation_deliveries'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY automation_scope ON %I USING (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true))) WITH CHECK (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))$p$,t);
 END LOOP;
END $$;

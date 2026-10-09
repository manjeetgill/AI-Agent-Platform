ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write',
 'calling:read','calling:write','telephony:read','telephony:write',
 'memory:read','memory:write','memory:review','alerts:send','notifications:read','notifications:send',
 'services:read','services:write']::text[]);
CREATE TABLE platform_service_choices (
 tenant_id text NOT NULL, app_id uuid NOT NULL, capability text NOT NULL CHECK(capability IN ('speech','email','whatsapp')),
 route text NOT NULL CHECK(route IN ('platform','provider')),
 provider text NOT NULL, revision integer NOT NULL CHECK(revision>0),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(app_id,capability),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id) ON DELETE CASCADE
);
CREATE TABLE platform_service_choice_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 tenant_id text NOT NULL, app_id uuid NOT NULL, capability text NOT NULL,
 route text NOT NULL, provider text NOT NULL, revision integer NOT NULL,
 actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id) ON DELETE CASCADE,
 UNIQUE(app_id,capability,revision)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['platform_service_choices','platform_service_choice_events'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY service_choice_scope ON %I USING (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true))) WITH CHECK (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)))$p$,t);
 END LOOP;
END $$;

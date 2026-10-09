CREATE TABLE platform_model_policies(
 app_id uuid PRIMARY KEY REFERENCES platform_apps(id),
 daily_budget_microusd bigint NOT NULL CHECK(daily_budget_microusd>=0),
 daily_calls integer NOT NULL CHECK(daily_calls>=0),
 allowed_providers text[] NOT NULL, sensitivity text NOT NULL DEFAULT 'restricted'
 CHECK(sensitivity IN ('public','internal','restricted'))
);
CREATE TABLE provider_chain_usage(
 app_id uuid NOT NULL REFERENCES platform_apps(id),day date NOT NULL,provider text NOT NULL,
 calls integer NOT NULL DEFAULT 0, reserved_microusd bigint NOT NULL DEFAULT 0,
 cooldown_until timestamptz,PRIMARY KEY(app_id,day,provider)
);
CREATE TABLE provider_chain_attempts(
 id uuid PRIMARY KEY,app_id uuid NOT NULL REFERENCES platform_apps(id),provider text NOT NULL,model text NOT NULL,
 day date NOT NULL, reserved_microusd bigint NOT NULL,estimated_microusd bigint,
 total_tokens integer,status text NOT NULL DEFAULT 'RESERVED',
 created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz
);
CREATE INDEX provider_attempts_app_time ON provider_chain_attempts(app_id,created_at);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['platform_model_policies','provider_chain_usage','provider_chain_attempts'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format($p$CREATE POLICY model_budget_scope ON %I USING(agent_worker_authorized() OR
 app_id::text=current_setting('agent.app',true)) WITH CHECK(agent_worker_authorized() OR
 app_id::text=current_setting('agent.app',true))$p$,t);
 END LOOP;
END $$;
-- A valid app ID cannot be combined with a different tenant. This restrictive
-- guard also covers explicit shared-read policies without removing their grants.
DO $$ DECLARE t record; BEGIN
 FOR t IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity
 AND c.relname NOT IN ('platform_apps','platform_organisations') LOOP
 EXECUTE format($p$CREATE POLICY request_scope_integrity ON %I AS RESTRICTIVE
 USING(agent_worker_authorized() OR coalesce(current_setting('agent.app',true),'')=''
 OR EXISTS(SELECT 1 FROM platform_apps a WHERE a.id::text=current_setting('agent.app',true)
 AND a.tenant_id=current_setting('agent.tenant',true)))
 WITH CHECK(agent_worker_authorized() OR coalesce(current_setting('agent.app',true),'')=''
 OR EXISTS(SELECT 1 FROM platform_apps a WHERE a.id::text=current_setting('agent.app',true)
 AND a.tenant_id=current_setting('agent.tenant',true)))$p$,t.relname);
 END LOOP;
END $$;

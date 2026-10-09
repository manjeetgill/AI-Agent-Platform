ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run',
 'knowledge:read','knowledge:write','knowledge:publish','channels:read','channels:write',
 'calling:read','calling:write','telephony:read','telephony:write',
 'memory:read','memory:write','memory:review','alerts:send','notifications:read','notifications:send']::text[]);
CREATE TABLE notification_email_jobs (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 request_key uuid NOT NULL, payload_hash text NOT NULL, payload_cipher text,
 template text NOT NULL, template_version integer NOT NULL DEFAULT 1,
 provider text NOT NULL CHECK(provider IN ('mock','resend','brevo')), config_hash text NOT NULL,
 status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','SENDING','RETRY','MOCK_SENT','ACCEPTED','FAILED','OUTCOME_UNKNOWN','EXPIRED','CONFIGURATION_REQUIRED')),
 attempts integer NOT NULL DEFAULT 0, may_have_sent boolean NOT NULL DEFAULT false,
 provider_id text, failure_code text, next_at timestamptz NOT NULL DEFAULT now(),
 lease_token uuid, lease_until timestamptz, first_attempt_at timestamptz,
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,request_key), UNIQUE(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE INDEX notification_email_due ON notification_email_jobs(next_at) WHERE status IN ('QUEUED','RETRY','SENDING','CONFIGURATION_REQUIRED');
CREATE INDEX notification_email_time ON notification_email_jobs(created_at);
CREATE INDEX notification_email_app_time ON notification_email_jobs(app_id,created_at);
CREATE INDEX notification_email_owner_time ON notification_email_jobs(app_id,owner_id,created_at DESC,id);
CREATE TABLE notification_email_attempts (
 job_id uuid NOT NULL, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 attempt integer NOT NULL, provider text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 outcome text NOT NULL DEFAULT 'SENDING', failure_code text, http_status integer, provider_id text,
 PRIMARY KEY(job_id,attempt),
 FOREIGN KEY(job_id,tenant_id,app_id,owner_id) REFERENCES notification_email_jobs(id,tenant_id,app_id,owner_id)
);
CREATE INDEX notification_email_attempt_app_time ON notification_email_attempts(app_id,started_at);
CREATE INDEX notification_email_attempt_time ON notification_email_attempts(started_at);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['notification_email_jobs','notification_email_attempts'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY notification_scope ON %I USING (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true))) WITH CHECK (
   current_setting('agent.worker',true)='on' OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))$p$,t);
 END LOOP;
END $$;

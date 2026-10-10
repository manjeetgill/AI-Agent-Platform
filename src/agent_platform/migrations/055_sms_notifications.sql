CREATE TABLE notification_sms_jobs (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 request_key uuid NOT NULL, payload_hash text NOT NULL, payload_cipher text,
 template text NOT NULL, template_version integer NOT NULL DEFAULT 1,
 provider text NOT NULL CHECK(provider IN ('mock','smsindiahub')), config_hash text NOT NULL,
 status text NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED','SENDING','RETRY','MOCK_SENT','ACCEPTED','FAILED','OUTCOME_UNKNOWN','EXPIRED','CONFIGURATION_REQUIRED')),
 attempts integer NOT NULL DEFAULT 0, may_have_sent boolean NOT NULL DEFAULT false,
 provider_id text, provider_job_id text, segments integer NOT NULL CHECK(segments BETWEEN 1 AND 10), failure_code text, next_at timestamptz NOT NULL DEFAULT now(),
 lease_token uuid, lease_until timestamptz, first_attempt_at timestamptz,
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,request_key), UNIQUE(id,tenant_id,app_id,owner_id),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE INDEX notification_sms_due ON notification_sms_jobs(next_at) WHERE status IN ('QUEUED','RETRY','SENDING','CONFIGURATION_REQUIRED');
CREATE INDEX notification_sms_time ON notification_sms_jobs(created_at);
CREATE INDEX notification_sms_app_time ON notification_sms_jobs(app_id,created_at);
CREATE INDEX notification_sms_owner_time ON notification_sms_jobs(app_id,owner_id,created_at DESC,id);
CREATE TABLE notification_sms_attempts (
 job_id uuid NOT NULL, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 attempt integer NOT NULL, provider text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 outcome text NOT NULL DEFAULT 'SENDING', failure_code text, http_status integer, provider_id text, provider_job_id text, segments integer NOT NULL CHECK(segments BETWEEN 1 AND 10),
 PRIMARY KEY(job_id,attempt),
 FOREIGN KEY(job_id,tenant_id,app_id,owner_id) REFERENCES notification_sms_jobs(id,tenant_id,app_id,owner_id)
);
CREATE INDEX notification_sms_attempt_app_time ON notification_sms_attempts(app_id,started_at);
CREATE INDEX notification_sms_attempt_time ON notification_sms_attempts(started_at);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['notification_sms_jobs','notification_sms_attempts'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format($p$CREATE POLICY notification_scope ON %I USING (
   agent_worker_authorized() OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true))) WITH CHECK (
   agent_worker_authorized() OR
   (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
    AND owner_id::text=current_setting('agent.owner',true)))$p$,t);
 END LOOP;
END $$;

CREATE FUNCTION agent_sms_counts(kind text, requested_app uuid)
RETURNS TABLE(daily bigint,monthly bigint,per_minute bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET agent.worker='on' AS $$
BEGIN
 IF requested_app IS NOT NULL AND requested_app::text IS DISTINCT FROM current_setting('agent.app',true)
 AND NOT coalesce(pg_has_role(session_user,to_regrole('agent_runtime_worker'),'member'),false)
 THEN RAISE EXCEPTION 'App scope required' USING ERRCODE='42501'; END IF;
 IF kind='notification_sms_jobs' THEN
 RETURN QUERY SELECT coalesce(sum(segments) FILTER(WHERE created_at>=date_trunc('day',now(),'UTC')),0)::bigint,
 coalesce(sum(segments) FILTER(WHERE created_at>=date_trunc('month',now(),'UTC')),0)::bigint,
 coalesce(sum(segments) FILTER(WHERE created_at>now()-interval '1 minute'),0)::bigint FROM public.notification_sms_jobs
 WHERE created_at>=least(date_trunc('month',now(),'UTC'),now()-interval '1 minute')
 AND (requested_app IS NULL OR app_id=requested_app);
 ELSIF kind='notification_sms_attempts' THEN
 RETURN QUERY SELECT coalesce(sum(segments) FILTER(WHERE started_at>=date_trunc('day',now(),'UTC')),0)::bigint,
 coalesce(sum(segments) FILTER(WHERE started_at>=date_trunc('month',now(),'UTC')),0)::bigint,
 coalesce(sum(segments) FILTER(WHERE started_at>now()-interval '1 minute'),0)::bigint FROM public.notification_sms_attempts
 WHERE started_at>=least(date_trunc('month',now(),'UTC'),now()-interval '1 minute')
 AND (requested_app IS NULL OR app_id=requested_app);
 ELSE RAISE EXCEPTION 'Unsupported quota'; END IF;
END $$;
REVOKE ALL ON FUNCTION agent_sms_counts(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_sms_counts(text,uuid) TO agent_runtime_api,agent_runtime_worker;

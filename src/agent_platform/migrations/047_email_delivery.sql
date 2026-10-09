ALTER TABLE notification_email_jobs ADD COLUMN recipient_hash text;
ALTER TABLE notification_email_jobs ADD COLUMN delivery_status text NOT NULL DEFAULT 'UNCONFIRMED';
ALTER TABLE notification_email_jobs ADD COLUMN delivery_at timestamptz;
ALTER TABLE notification_email_jobs DROP CONSTRAINT notification_email_jobs_status_check;
ALTER TABLE notification_email_jobs ADD CHECK(status IN ('QUEUED','SENDING','RETRY','MOCK_SENT','ACCEPTED','FAILED','OUTCOME_UNKNOWN','EXPIRED','CONFIGURATION_REQUIRED','SUPPRESSED'));
CREATE INDEX notification_email_provider ON notification_email_jobs(app_id,provider,provider_id);
CREATE INDEX notification_email_provider_normalized ON notification_email_jobs(app_id,provider,(trim(both '<>' from provider_id)));
CREATE INDEX notification_email_recipient ON notification_email_jobs(app_id,recipient_hash);
CREATE TABLE notification_email_events (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL,
 provider text NOT NULL CHECK(provider IN ('resend','brevo')), event_key text NOT NULL,
 provider_id text NOT NULL, recipient_hash text NOT NULL, outcome text NOT NULL,
 occurred_at timestamptz NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
 job_id uuid, owner_id uuid,
 UNIQUE(app_id,provider,event_key),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(job_id,tenant_id,app_id,owner_id) REFERENCES notification_email_jobs(id,tenant_id,app_id,owner_id)
);
CREATE INDEX notification_email_event_pending ON notification_email_events(app_id,provider,provider_id) WHERE job_id IS NULL;
CREATE INDEX notification_email_event_expiry ON notification_email_events(received_at) WHERE job_id IS NULL;
CREATE INDEX notification_email_event_job ON notification_email_events(job_id,occurred_at);
CREATE TABLE notification_email_suppressions (
 tenant_id text NOT NULL, app_id uuid NOT NULL, recipient_hash text NOT NULL,
 reason text NOT NULL, event_id uuid NOT NULL REFERENCES notification_email_events(id),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(app_id,recipient_hash),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id)
);
ALTER TABLE notification_email_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_email_events FORCE ROW LEVEL SECURITY;
CREATE POLICY email_event_scope ON notification_email_events USING (
 current_setting('agent.worker',true)='on' OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
 AND owner_id::text=current_setting('agent.owner',true))) WITH CHECK (current_setting('agent.worker',true)='on');
ALTER TABLE notification_email_suppressions ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_email_suppressions FORCE ROW LEVEL SECURITY;
CREATE POLICY email_suppression_scope ON notification_email_suppressions USING (current_setting('agent.worker',true)='on') WITH CHECK (current_setting('agent.worker',true)='on');

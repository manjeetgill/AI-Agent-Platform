CREATE TABLE feed_connections (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, app_id uuid NOT NULL, owner_id uuid NOT NULL,
 profile text NOT NULL, profile_hash text NOT NULL,
 revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
 credential_cipher text, disabled boolean NOT NULL DEFAULT false,
 state text NOT NULL DEFAULT 'AUTHORIZATION_REQUIRED', reason_code text NOT NULL DEFAULT 'credentials_missing',
 test_task_id uuid REFERENCES chore_tasks(id), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(app_id,owner_id,profile),
 FOREIGN KEY(app_id,tenant_id) REFERENCES platform_apps(id,tenant_id),
 FOREIGN KEY(app_id,owner_id) REFERENCES platform_external_users(app_id,owner_id)
);
CREATE TABLE feed_request_usage (
 app_id uuid NOT NULL REFERENCES platform_apps(id), hour timestamptz NOT NULL,
 requests integer NOT NULL CHECK(requests>=0), PRIMARY KEY(app_id,hour)
);
ALTER TABLE platform_app_policies ADD COLUMN feed_requests_per_hour integer NOT NULL DEFAULT 60
 CHECK(feed_requests_per_hour BETWEEN 0 AND 10000);
ALTER TABLE feed_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE feed_connections FORCE ROW LEVEL SECURITY;
CREATE POLICY feed_scope ON feed_connections USING (
 current_setting('agent.worker',true)='on' OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
 AND owner_id::text=current_setting('agent.owner',true))) WITH CHECK (
 current_setting('agent.worker',true)='on' OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id::text=current_setting('agent.app',true)
 AND owner_id::text=current_setting('agent.owner',true)));

CREATE TABLE delegation_replays(
 issuer text NOT NULL,jti text NOT NULL,tenant_id text NOT NULL,app_id text NOT NULL DEFAULT '',
 owner_id text NOT NULL,expires_at timestamptz NOT NULL,PRIMARY KEY(issuer,jti)
);
CREATE INDEX delegation_expiry ON delegation_replays(expires_at);
ALTER TABLE delegation_replays ENABLE ROW LEVEL SECURITY;
ALTER TABLE delegation_replays FORCE ROW LEVEL SECURITY;
CREATE POLICY delegation_scope ON delegation_replays USING(agent_worker_authorized() OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id=coalesce(current_setting('agent.app',true),'')
 AND owner_id=current_setting('agent.owner',true))) WITH CHECK(agent_worker_authorized() OR
 (tenant_id=current_setting('agent.tenant',true) AND app_id=coalesce(current_setting('agent.app',true),'')
 AND owner_id=current_setting('agent.owner',true)));

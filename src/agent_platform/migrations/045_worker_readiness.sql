-- Global operational metadata only; no tenant data, hostnames or credentials.
CREATE TABLE platform_worker_heartbeats (
 instance_id uuid PRIMARY KEY,
 tool_versions jsonb NOT NULL CHECK(jsonb_typeof(tool_versions)='object' AND octet_length(tool_versions::text)<=32000),
 server_renderer boolean NOT NULL DEFAULT false,
 last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX platform_worker_freshness ON platform_worker_heartbeats(last_seen_at);
ALTER TABLE platform_worker_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_worker_heartbeats FORCE ROW LEVEL SECURITY;
CREATE POLICY worker_readiness_read ON platform_worker_heartbeats FOR SELECT USING (true);
CREATE POLICY worker_readiness_write ON platform_worker_heartbeats FOR ALL
 USING (current_setting('agent.worker',true)='on')
 WITH CHECK (current_setting('agent.worker',true)='on');

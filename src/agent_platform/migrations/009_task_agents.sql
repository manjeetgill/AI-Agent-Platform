ALTER TABLE platform_service_keys DROP CONSTRAINT platform_service_keys_scopes_check;
ALTER TABLE platform_service_keys ADD CHECK(scopes <@ ARRAY[
 'tasks:read','tasks:write','tasks:approve','agents:read','agents:write','agents:run']::text[]);
CREATE TABLE platform_tool_grants (
 app_id uuid NOT NULL REFERENCES platform_apps(id), tool text NOT NULL,
 PRIMARY KEY(app_id,tool)
);
CREATE TABLE task_agents (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id uuid NOT NULL,
 app_id uuid NOT NULL REFERENCES platform_apps(id), version integer NOT NULL DEFAULT 1,
 blueprint jsonb NOT NULL, request_key text NOT NULL, definition_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(app_id,owner_id,request_key)
);
CREATE INDEX task_agents_owner ON task_agents(app_id,owner_id,created_at DESC,id DESC);
ALTER TABLE chore_tasks ADD COLUMN source_agent_id uuid REFERENCES task_agents(id);
ALTER TABLE chore_tasks ADD COLUMN source_app_id uuid REFERENCES platform_apps(id);
ALTER TABLE chore_tasks ADD COLUMN invocation_hash text;
CREATE TABLE agent_planning_usage (
 app_id uuid NOT NULL REFERENCES platform_apps(id), owner_id uuid NOT NULL,
 day date NOT NULL DEFAULT CURRENT_DATE, calls integer NOT NULL DEFAULT 0,
 PRIMARY KEY(app_id,owner_id,day)
);
CREATE TABLE app_planning_usage (
 app_id uuid NOT NULL REFERENCES platform_apps(id), day date NOT NULL DEFAULT CURRENT_DATE,
 calls integer NOT NULL DEFAULT 0, PRIMARY KEY(app_id,day)
);
-- Existing service-user tasks acquire the same grant checks as new ones.
UPDATE chore_tasks t SET source_app_id=u.app_id FROM platform_external_users u
 JOIN platform_apps a ON a.id=u.app_id WHERE t.owner_id=u.owner_id AND t.tenant_id=a.tenant_id;

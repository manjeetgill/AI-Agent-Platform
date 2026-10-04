CREATE TABLE agent_idea_usage (
 tenant_id text NOT NULL, owner_id uuid NOT NULL, usage_day date NOT NULL DEFAULT CURRENT_DATE,
 calls integer NOT NULL DEFAULT 1 CHECK (calls BETWEEN 1 AND 20),
 PRIMARY KEY(tenant_id,owner_id,usage_day)
);

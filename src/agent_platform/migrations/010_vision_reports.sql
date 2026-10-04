CREATE TABLE IF NOT EXISTS agent_vision_reports (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_id uuid NOT NULL,
    question text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('chart', 'document')),
    report jsonb NOT NULL,
    model text NOT NULL,
    tokens integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_vision_owner ON agent_vision_reports(tenant_id, owner_id, created_at DESC);

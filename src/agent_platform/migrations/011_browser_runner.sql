CREATE TABLE browser_devices (
 id uuid PRIMARY KEY, app_id uuid NOT NULL REFERENCES platform_apps(id), owner_id uuid NOT NULL,
 secret_hash text NOT NULL, expires_at timestamptz NOT NULL DEFAULT now()+interval '1 day', revoked_at timestamptz
);
CREATE TABLE browser_commands (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES chore_tasks(id), attempt_id uuid NOT NULL UNIQUE,
 arguments jsonb NOT NULL, state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','STARTED','DONE')),
 device_id uuid REFERENCES browser_devices(id), result jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX browser_pending ON browser_commands(created_at) WHERE state='PENDING';

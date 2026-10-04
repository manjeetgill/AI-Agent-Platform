CREATE TABLE chore_tasks (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, owner_id uuid NOT NULL,
 request text NOT NULL, request_key text NOT NULL,
 status text NOT NULL DEFAULT 'DRAFT' CHECK(status IN
 ('DRAFT','READY','RUNNING','WAITING_APPROVAL','WAITING_USER','PAUSED','OUTCOME_UNKNOWN','DONE','FAILED','CANCELLED')),
 current_step integer NOT NULL DEFAULT 0, revision integer NOT NULL DEFAULT 0,
 stop_requested text CHECK(stop_requested IN ('PAUSED','CANCELLED')),
 lease_token uuid, lease_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,owner_id,request_key)
);
CREATE UNIQUE INDEX chore_one_active_owner ON chore_tasks(tenant_id,owner_id)
 WHERE status NOT IN ('DRAFT','DONE','FAILED','CANCELLED');
CREATE INDEX chore_owner_history ON chore_tasks(tenant_id,owner_id,created_at DESC,id);
CREATE INDEX chore_expired_lease ON chore_tasks(lease_until) WHERE status='RUNNING';
CREATE TABLE chore_steps (
 task_id uuid NOT NULL REFERENCES chore_tasks(id), position integer NOT NULL,
 tool text NOT NULL, tool_version text NOT NULL, arguments jsonb NOT NULL,
 action_hash text NOT NULL, summary text NOT NULL, risk text NOT NULL
 CHECK(risk IN ('READ','SEND','USER_ONLY')),
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','DONE','UNKNOWN','FAILED')),
 attempts integer NOT NULL DEFAULT 0, result jsonb,
 PRIMARY KEY(task_id,position)
);
CREATE TABLE chore_approvals (
 id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES chore_tasks(id), position integer NOT NULL,
 action_hash text NOT NULL, approved_by uuid NOT NULL, expires_at timestamptz NOT NULL,
 consumed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chore_approval_lookup ON chore_approvals(task_id,position,action_hash);
-- This table IS the durable work queue. Its rows commit with task state changes.
CREATE TABLE chore_jobs (
 task_id uuid PRIMARY KEY REFERENCES chore_tasks(id),
 mode text NOT NULL DEFAULT 'RUN' CHECK(mode IN ('RUN','RECONCILE')),
 available_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE chore_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 task_id uuid NOT NULL REFERENCES chore_tasks(id), event text NOT NULL,
 details jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chore_event_history ON chore_events(task_id,id);

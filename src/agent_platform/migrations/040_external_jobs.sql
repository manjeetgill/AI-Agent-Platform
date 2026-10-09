-- Waiting for an acknowledged domain job uses the existing durable queue.
ALTER TABLE chore_jobs DROP CONSTRAINT chore_jobs_mode_check;
ALTER TABLE chore_jobs ADD CONSTRAINT chore_jobs_mode_check CHECK(mode IN ('RUN','RECONCILE','POLL'));
ALTER TABLE chore_steps ADD COLUMN external_job_reference jsonb;
ALTER TABLE chore_steps ADD COLUMN external_job_deadline timestamptz;
ALTER TABLE chore_steps ADD COLUMN external_job_polls integer NOT NULL DEFAULT 0 CHECK(external_job_polls>=0);
ALTER TABLE chore_steps ADD COLUMN external_job_failures integer NOT NULL DEFAULT 0 CHECK(external_job_failures>=0);
ALTER TABLE chore_steps ADD COLUMN external_job_state text CHECK(external_job_state IN
 ('RUNNING','SUCCEEDED','FAILED','TIMED_OUT','UNAVAILABLE','DETACHED'));
ALTER TABLE chore_steps ADD CONSTRAINT chore_job_reference_size
 CHECK(external_job_reference IS NULL OR (jsonb_typeof(external_job_reference)='object' AND octet_length(external_job_reference::text)<=4000));

ALTER TABLE agent_runs ADD COLUMN lease_until timestamptz;
CREATE INDEX agent_run_lease ON agent_runs(lease_until) WHERE status IN ('CREATED','RUNNING');

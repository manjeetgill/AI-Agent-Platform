-- External uncertainty survives scheduling, takeover, and process restarts.
ALTER TABLE chore_steps ADD COLUMN outcome_pending boolean NOT NULL DEFAULT false;
UPDATE chore_steps s SET outcome_pending=true FROM chore_tasks t
 WHERE s.task_id=t.id AND s.position=t.current_step AND s.risk<>'READ'
 AND (s.status IN ('RUNNING','UNKNOWN') OR t.status IN ('WAITING_USER','OUTCOME_UNKNOWN')
 OR EXISTS(SELECT 1 FROM chore_jobs j WHERE j.task_id=t.id AND j.mode='RECONCILE'));

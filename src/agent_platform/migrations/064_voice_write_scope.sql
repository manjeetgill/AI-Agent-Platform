-- Shared agent visibility must not confer configuration history write authority.
DROP POLICY version_scope ON web_voice_versions;
CREATE POLICY version_read ON web_voice_versions FOR SELECT USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM web_voice_agents a WHERE a.id=agent_id));
CREATE POLICY version_write ON web_voice_versions USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM web_voice_agents a WHERE a.id=agent_id AND a.owner_id::text=current_setting('agent.owner',true)));
-- App totals can be read for quotas; only an authorized session can change usage.
DROP POLICY usage_scope ON voice_usage_ledger;
CREATE POLICY usage_read ON voice_usage_ledger FOR SELECT USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM platform_apps a WHERE a.id=app_id));
CREATE POLICY usage_write ON voice_usage_ledger USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM web_voice_sessions s WHERE s.id=session_id AND s.app_id=voice_usage_ledger.app_id));

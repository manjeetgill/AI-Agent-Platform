-- New voice state inherits the production tenant isolation model.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['web_voice_versions','web_voice_events','web_voice_transcripts','voice_usage_ledger','voice_spend_limits','voice_rate_cards'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 END LOOP;
END $$;
CREATE POLICY version_scope ON web_voice_versions USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM web_voice_agents a WHERE a.id=agent_id));
CREATE POLICY event_scope ON web_voice_events USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM web_voice_sessions s WHERE s.id=session_id));
CREATE POLICY transcript_scope ON web_voice_transcripts USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM web_voice_sessions s WHERE s.id=session_id));
-- Costs are app aggregates; no prompts or caller content in this ledger.
CREATE POLICY usage_scope ON voice_usage_ledger USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM platform_apps a WHERE a.id=app_id));
CREATE POLICY limits_read ON voice_spend_limits FOR SELECT USING(agent_worker_authorized() OR EXISTS(SELECT 1 FROM platform_apps a WHERE a.id=app_id));
CREATE POLICY rates_read ON voice_rate_cards FOR SELECT USING(true);
-- Only the migration/operator owner may change rates or spending authority.
CREATE POLICY limits_operator ON voice_spend_limits USING(current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='voice_spend_limits'::regclass)));
CREATE POLICY rates_operator ON voice_rate_cards USING(current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='voice_rate_cards'::regclass)));
-- Return identity only; signed internal authorization is validated before use.
CREATE FUNCTION agent_media_identity(sid uuid, app uuid, caller uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public SET agent.worker='on' AS $$
 SELECT jsonb_build_object('app_id',s.app_id,'user',s.owner_id,'tenant',a.tenant_id,
 'voice_agent',s.agent_id,'voice_channel',c.channel_id,'voice_conversation',h.conversation_id)
 FROM public.web_voice_sessions s JOIN public.platform_apps a ON a.id=s.app_id
 LEFT JOIN public.web_voice_handoffs h ON h.id=s.handoff_id
 LEFT JOIN public.whatsapp_conversations c ON c.id=h.conversation_id
 WHERE s.id=sid AND s.app_id=app AND s.owner_id=caller AND s.media_remote AND s.state='STARTING' AND s.expires_at>now()
$$;
REVOKE ALL ON FUNCTION agent_media_identity(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_media_identity(uuid,uuid,uuid) TO agent_runtime_api,agent_runtime_worker;
-- Global admission must see capacity across apps without exposing their calls.
-- Caller holds the existing global transaction advisory lock through insertion.
CREATE FUNCTION agent_voice_counts(requested_app uuid) RETURNS TABLE(active bigint,daily bigint,monthly bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET agent.worker='on' AS $$
BEGIN
 IF requested_app::text IS DISTINCT FROM current_setting('agent.app',true)
 AND NOT coalesce(pg_has_role(session_user,to_regrole('agent_runtime_worker'),'member'),false)
 THEN RAISE EXCEPTION 'App scope required' USING ERRCODE='42501'; END IF;
 UPDATE public.web_voice_sessions SET state='UNKNOWN',ended_at=now() WHERE state IN ('STARTING','ACTIVE')
 AND (expires_at<now() OR (media_remote AND media_heartbeat<now()-interval '10 seconds'));
 RETURN QUERY SELECT count(*) FILTER(WHERE state IN ('STARTING','ACTIVE')),
 count(*) FILTER(WHERE app_id=requested_app AND created_at>now()-interval '24 hours'),
 count(*) FILTER(WHERE app_id=requested_app AND created_at>=date_trunc('month',now())) FROM public.web_voice_sessions;
END $$;
REVOKE ALL ON FUNCTION agent_voice_counts(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_voice_counts(uuid) TO agent_runtime_api,agent_runtime_worker;

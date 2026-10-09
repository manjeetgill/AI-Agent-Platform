-- Fixed, credential-checked identity bootstrap. This function exposes no key hash.
CREATE FUNCTION agent_authenticate_service(key_id uuid, digest text, external_user text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public
SET agent.worker='on' AS $$
DECLARE k record; owner uuid;
BEGIN
 IF length(external_user) NOT BETWEEN 1 AND 200 OR external_user ~ '[[:cntrl:]]' THEN RETURN NULL; END IF;
 SELECT s.*,a.tenant_id INTO k FROM public.platform_service_keys s JOIN public.platform_apps a ON a.id=s.app_id
 WHERE s.id=key_id AND s.secret_hash=digest AND s.revoked_at IS NULL AND s.expires_at>now();
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT owner_id INTO owner FROM public.platform_external_users WHERE app_id=k.app_id AND external_id=external_user;
 IF owner IS NULL THEN
  INSERT INTO public.platform_external_users(app_id,external_id,owner_id)
  VALUES(k.app_id,external_user,gen_random_uuid()) ON CONFLICT DO NOTHING;
  SELECT owner_id INTO owner FROM public.platform_external_users WHERE app_id=k.app_id AND external_id=external_user;
 END IF;
 RETURN jsonb_build_object('tenant',k.tenant_id,'app_id',k.app_id,'user',owner,
 'principal','service:'||key_id,'scopes',k.scopes);
END $$;
REVOKE ALL ON FUNCTION agent_authenticate_service(uuid,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_authenticate_service(uuid,text,text) TO agent_runtime_api,agent_runtime_worker;
-- Bootstrap identifiers for capability-authenticated callbacks. No business data,
-- keys or secrets are returned. Handlers verify signatures before mutation.
CREATE FUNCTION agent_connection_identity(kind text, ident text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET agent.worker='on' AS $$
DECLARE r record; voice_channel text; voice_agent text; voice_conversation text;
BEGIN
 CASE kind
 WHEN 'email' THEN SELECT id AS app_id,NULL::uuid AS owner_id INTO r FROM public.platform_apps WHERE id::text=ident;
 WHEN 'meta_phone' THEN SELECT app_id,owner_id INTO r FROM public.meta_connections WHERE phone_id=ident LIMIT 1;
 WHEN 'meta_waba' THEN SELECT app_id,owner_id INTO r FROM public.meta_waba_owners WHERE waba_id=ident LIMIT 1;
 WHEN 'channel' THEN SELECT app_id,owner_id INTO r FROM public.whatsapp_channels WHERE id::text=ident;
 WHEN 'gateway' THEN SELECT app_id,owner_id INTO r FROM public.telephony_connections WHERE id::text=ident;
 WHEN 'browser' THEN SELECT app_id,owner_id INTO r FROM public.browser_devices WHERE id::text=ident;
 WHEN 'retailer' THEN SELECT app_id,owner_id INTO r FROM public.retailer_browser_devices WHERE id::text=ident;
 WHEN 'signup' THEN SELECT app_id,owner_id INTO r FROM public.meta_signup_sessions WHERE id::text=ident;
 WHEN 'voice' THEN SELECT app_id,owner_id INTO r FROM public.web_voice_handoffs WHERE token_hash=ident AND expires_at>now();
 ELSE RETURN NULL;
 END CASE;
 IF r.app_id IS NULL THEN RETURN NULL; END IF;
 IF kind='voice' THEN
 SELECT c.channel_id::text,h.agent_id::text,h.conversation_id::text INTO voice_channel,voice_agent,voice_conversation
 FROM public.web_voice_handoffs h JOIN public.whatsapp_conversations c ON c.id=h.conversation_id WHERE h.token_hash=ident;
 END IF;
 RETURN (SELECT jsonb_build_object('app_id',r.app_id,'user',r.owner_id,'tenant',tenant_id,'email_callback',kind='email','voice_channel',voice_channel,'voice_agent',voice_agent,'voice_conversation',voice_conversation)
 FROM public.platform_apps WHERE id=r.app_id);
END $$;
REVOKE ALL ON FUNCTION agent_connection_identity(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_connection_identity(text,text) TO agent_runtime_api,agent_runtime_worker;
-- Existing app-published documents/memories can be read, but only owners write.
CREATE POLICY document_shared_read ON knowledge_documents FOR SELECT USING (
 app_id::text=current_setting('agent.app',true) AND visibility='app');
CREATE POLICY memory_shared_read ON platform_memories FOR SELECT USING (
 app_id::text=current_setting('agent.app',true) AND visibility='app' AND status='VALIDATED');

DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['notification_email_jobs','notification_email_events','notification_email_suppressions'] LOOP
 EXECUTE format($p$CREATE POLICY signed_email_callback ON %I USING (
 app_id::text=current_setting('agent.email_callback',true)) WITH CHECK (
 app_id::text=current_setting('agent.email_callback',true))$p$,t);
 END LOOP;
END $$;

CREATE POLICY suppression_app_read ON notification_email_suppressions FOR SELECT USING (app_id::text=current_setting('agent.app',true));
-- A child can detect its own parent link to enforce the no-nesting rule.
CREATE POLICY business_child_read ON platform_business_workspaces FOR SELECT USING (
 app_id::text=current_setting('agent.app',true));
-- Parent backends can rotate credentials only for businesses owned by this user.
CREATE POLICY business_child_keys ON platform_service_keys USING (
 EXISTS(SELECT 1 FROM platform_business_workspaces w WHERE w.app_id=platform_service_keys.app_id
 AND w.parent_app_id::text=current_setting('agent.app',true) AND w.owner_id::text=current_setting('agent.owner',true)))
 WITH CHECK (EXISTS(SELECT 1 FROM platform_business_workspaces w WHERE w.app_id=platform_service_keys.app_id
 AND w.parent_app_id::text=current_setting('agent.app',true) AND w.owner_id::text=current_setting('agent.owner',true)));

CREATE POLICY voice_shared_agent ON web_voice_agents FOR SELECT USING (
 app_id::text=current_setting('agent.app',true) AND
 (visibility='app' OR id::text=current_setting('agent.voice_agent',true)));
CREATE POLICY voice_channel_read ON whatsapp_channels FOR SELECT USING (
 app_id::text=current_setting('agent.app',true) AND id::text=current_setting('agent.voice_channel',true));
CREATE POLICY voice_conversation_limit ON whatsapp_conversations AS RESTRICTIVE USING (
 coalesce(current_setting('agent.voice_conversation',true),'')='' OR id::text=current_setting('agent.voice_conversation',true));
CREATE POLICY authorized_alert_sender_read ON whatsapp_channels FOR SELECT USING (
 EXISTS(SELECT 1 FROM whatsapp_alert_routes r WHERE r.channel_id=whatsapp_channels.id
 AND r.app_id::text=current_setting('agent.app',true) AND r.enabled));
-- Quota checks need totals across owners; return counts only, never job payloads.
CREATE FUNCTION agent_email_counts(kind text, requested_app uuid)
RETURNS TABLE(daily bigint,monthly bigint,per_minute bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET agent.worker='on' AS $$
BEGIN
 IF requested_app IS NOT NULL AND requested_app::text IS DISTINCT FROM current_setting('agent.app',true)
 AND NOT coalesce(pg_has_role(session_user,to_regrole('agent_runtime_worker'),'member'),false)
 THEN RAISE EXCEPTION 'App scope required' USING ERRCODE='42501'; END IF;
 IF kind='notification_email_jobs' THEN
 RETURN QUERY SELECT count(*) FILTER(WHERE created_at>=date_trunc('day',now(),'UTC')),
 count(*) FILTER(WHERE created_at>=date_trunc('month',now(),'UTC')),
 count(*) FILTER(WHERE created_at>now()-interval '1 minute') FROM public.notification_email_jobs
 WHERE created_at>=least(date_trunc('month',now(),'UTC'),now()-interval '1 minute')
 AND (requested_app IS NULL OR app_id=requested_app);
 ELSIF kind='notification_email_attempts' THEN
 RETURN QUERY SELECT count(*) FILTER(WHERE started_at>=date_trunc('day',now(),'UTC')),
 count(*) FILTER(WHERE started_at>=date_trunc('month',now(),'UTC')),
 count(*) FILTER(WHERE started_at>now()-interval '1 minute') FROM public.notification_email_attempts
 WHERE started_at>=least(date_trunc('month',now(),'UTC'),now()-interval '1 minute')
 AND (requested_app IS NULL OR app_id=requested_app);
 ELSE RAISE EXCEPTION 'Unsupported quota'; END IF;
END $$;
REVOKE ALL ON FUNCTION agent_email_counts(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_email_counts(text,uuid) TO agent_runtime_api,agent_runtime_worker;
-- Channel preview state has a stable channel owner separate from operator identity.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['knowledge_answers','agent_planning_usage'] LOOP
 EXECUTE format($p$CREATE POLICY channel_preview_scope ON %I USING (
 app_id::text=current_setting('agent.app',true) AND EXISTS(SELECT 1 FROM whatsapp_channels c
 WHERE c.id=%I.owner_id AND c.app_id=%I.app_id AND c.owner_id::text=current_setting('agent.owner',true)))
 WITH CHECK (app_id::text=current_setting('agent.app',true) AND EXISTS(SELECT 1 FROM whatsapp_channels c
 WHERE c.id=%I.owner_id AND c.app_id=%I.app_id AND c.owner_id::text=current_setting('agent.owner',true)))$p$,t,t,t,t,t);
 END LOOP;
END $$;
-- A command is scoped through its task before a browser claims it; device_id
-- is legitimately NULL until dispatch. Once assigned, the device must match.
ALTER POLICY tenant_scope ON browser_commands USING (
 agent_worker_authorized() OR (EXISTS(SELECT 1 FROM chore_tasks t WHERE t.id=browser_commands.task_id)
 AND (device_id IS NULL OR EXISTS(SELECT 1 FROM browser_devices d WHERE d.id=browser_commands.device_id))))
 WITH CHECK (agent_worker_authorized() OR (EXISTS(SELECT 1 FROM chore_tasks t WHERE t.id=browser_commands.task_id)
 AND (device_id IS NULL OR EXISTS(SELECT 1 FROM browser_devices d WHERE d.id=browser_commands.device_id))));

-- Runtime role membership, not a caller-set flag alone, authorizes workers.
CREATE FUNCTION agent_worker_authorized() RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT current_setting('agent.worker',true)='on'
 AND coalesce(pg_has_role(current_user,to_regrole('agent_runtime_worker'),'member'),false)
$$;
-- Harden the existing policies without altering historical migrations.
DO $$ DECLARE p record; u text; c text; BEGIN
 FOR p IN SELECT polname,polrelid::regclass AS tab,pg_get_expr(polqual,polrelid) AS u,
 pg_get_expr(polwithcheck,polrelid) AS c FROM pg_policy LOOP
  u:=replace(p.u, 'current_setting(''agent.worker''::text, true) = ''on''::text', 'agent_worker_authorized()');
  c:=replace(p.c, 'current_setting(''agent.worker''::text, true) = ''on''::text', 'agent_worker_authorized()');
  EXECUTE format('ALTER POLICY %I ON %s %s %s',p.polname,p.tab,
   CASE WHEN u IS NULL THEN '' ELSE 'USING ('||u||')' END,
   CASE WHEN c IS NULL THEN '' ELSE 'WITH CHECK ('||c||')' END);
 END LOOP;
END $$;
-- Each table receives an explicit policy. Parent references are derived from
-- real foreign keys; an unknown table with no ownership path aborts migration.
DO $$ DECLARE t record; cols text[]; expr text; checks text[]; fk record; parent text;
BEGIN
 FOR t IN SELECT c.oid,c.relname,c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname LOOP
  IF t.relname=ANY(ARRAY['agent_schema_migrations','platform_worker_heartbeats','whatsapp_worker_health',
   'whatsapp_call_worker_health','meta_webhook_receipts','meta_platform_webhook']) THEN CONTINUE; END IF;
  IF t.relrowsecurity THEN CONTINUE; END IF;
  SELECT array_agg(attname::text) INTO cols FROM pg_attribute WHERE attrelid=t.oid AND attnum>0 AND NOT attisdropped;
  checks:=ARRAY[]::text[];
  IF t.relname='platform_organisations' THEN
   checks:=array_append(checks,'id::text=current_setting(''agent.tenant'',true)');
  ELSIF 'tenant_id'=ANY(cols) THEN
   checks:=array_append(checks,'tenant_id::text=current_setting(''agent.tenant'',true)');
  END IF;
  IF t.relname='platform_apps' THEN
   checks:=array_append(checks,'id::text=current_setting(''agent.app'',true)');
  ELSIF 'parent_app_id'=ANY(cols) THEN
   checks:=array_append(checks,'parent_app_id::text=current_setting(''agent.app'',true)');
  ELSIF 'app_id'=ANY(cols) THEN
   checks:=array_append(checks,$e$coalesce(app_id::text,'')=coalesce(current_setting('agent.app',true),'')$e$);
  ELSIF 'source_app_id'=ANY(cols) THEN
   checks:=array_append(checks,$e$coalesce(source_app_id::text,'')=coalesce(current_setting('agent.app',true),'')$e$);
  END IF;
  IF 'owner_id'=ANY(cols) THEN
   checks:=array_append(checks,'owner_id::text=current_setting(''agent.owner'',true)');
  END IF;
  IF cardinality(checks)=0 THEN
   FOR fk IN SELECT conrelid,confrelid,conkey,confkey FROM pg_constraint WHERE conrelid=t.oid AND contype='f' LOOP
    SELECT string_agg(format('p.%I=%I.%I',pa.attname,t.relname,ca.attname),' AND ') INTO parent
     FROM unnest(fk.conkey,fk.confkey) AS k(child,par)
     JOIN pg_attribute ca ON ca.attrelid=fk.conrelid AND ca.attnum=k.child
     JOIN pg_attribute pa ON pa.attrelid=fk.confrelid AND pa.attnum=k.par;
    checks:=array_append(checks,format('EXISTS(SELECT 1 FROM %s p WHERE %s)',fk.confrelid::regclass,parent));
   END LOOP;
   IF t.relname='whatsapp_dispatches' THEN checks:=ARRAY['EXISTS(SELECT 1 FROM whatsapp_messages p WHERE p.id=whatsapp_dispatches.message_id)']; END IF;
  END IF;
  IF cardinality(checks)=0 THEN RAISE EXCEPTION 'Unclassified table %',t.relname; END IF;
  expr:='agent_worker_authorized() OR ('||array_to_string(checks,' AND ')||')';
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t.relname);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t.relname);
  EXECUTE format('CREATE POLICY tenant_scope ON %I USING (%s) WITH CHECK (%s)',t.relname,expr,expr);
 END LOOP;
END $$;

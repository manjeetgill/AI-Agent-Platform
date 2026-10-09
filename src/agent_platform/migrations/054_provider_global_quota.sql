-- Only aggregate operational counters cross app scopes; no prompts, user IDs,
-- task results or credentials are exposed. The provider advisory lock is held
-- by reserve() until its app-scoped reservation commits.
CREATE FUNCTION agent_provider_quota(provider_name text, usage_day date)
RETURNS TABLE(reserved_microusd bigint, cooling boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public SET agent.worker='on' AS $$
 SELECT coalesce(sum(reserved_microusd) FILTER(WHERE day=usage_day),0)::bigint,
 coalesce(bool_or(cooldown_until>now()),false)
 FROM public.provider_chain_usage WHERE provider=provider_name
$$;
REVOKE ALL ON FUNCTION agent_provider_quota(text,date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_provider_quota(text,date) TO agent_runtime_api,agent_runtime_worker;

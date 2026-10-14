"""Read-only voice deployment preflight. Exit nonzero for unmet release gates.

No production writes, calls, messages, secrets, or firewall changes. An optional
public origin is probed with ordinary TLS verification and no credentials.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform import migrate, voice_config, voice_ice, web_voice


async def run(config_path, origin=None):
    if config_path:
        if config_path.stat().st_mode & 0o077:
            raise ValueError('Private config must have mode 0600')
        os.environ.update(json.loads(config_path.read_text()))
    checks=[]
    def result(name, passed, detail):
        checks.append({'check':name,'status':'PASS' if passed else 'BLOCKED','detail':detail})
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], open=False, kwargs={'row_factory':dict_row}) as pool:
        async with pool.connection() as conn:
            result('migration_manifest',await migrate.schema_ready(conn),'Database must exactly match this release')
            role=await (await conn.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user')).fetchone()
            owner=await (await conn.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tableowner=current_user LIMIT 1")).fetchone()
            result('runtime_database_role',not any(role.values()) and not owner,'Runtime must not be a database superuser, role creator, RLS bypass role or table owner')
            active=await (await conn.execute("SELECT count(*) n FROM web_voice_sessions WHERE state IN ('STARTING','ACTIVE') AND expires_at>now()")).fetchone()
            result('drain_before_release',active['n']==0,'Do not restart a media process during active calls')
        with voice_config.using(None):
            result('audio_configuration',web_voice.ready(),'Selected model and speech assets must be available; no paid request made')
        try:
            relay=voice_ice.servers('deployment-probe')
            result('turn_configuration',bool(relay),'TURN must be configured and then verified by an actual relay-only call')
        except (ValueError,TypeError):
            result('turn_configuration',False,'TURN configuration invalid; credentials omitted')
    async with httpx.AsyncClient(timeout=10,follow_redirects=False) as client:
        try:
            response=await client.get('http://127.0.0.1:4160/readiness')
            result('local_api',response.status_code==200,'Isolated voice API readiness')
        except httpx.HTTPError:
            result('local_api',False,'Isolated voice API unavailable')
        if origin:
            parsed=urlsplit(origin)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
                raise ValueError('Use an HTTPS origin without credentials, paths or query')
            for path,expected,label in [('/voice/call',{200},'public_voice_page'),('/v1/voice/agents',{401,403},'unauthenticated_agent_api'),('/v1/voice/sessions',{401,403},'unauthenticated_session_api'),('/v1/voice/usage',{401,403},'unauthenticated_usage_api'),('/console/api/voice/agents',{404},'private_workspace_hidden')]:
                try:
                    response=await client.get(origin.rstrip('/')+path)
                    result(label,response.status_code in expected,f'HTTP {response.status_code}; expected {sorted(expected)}')
                except httpx.HTTPError:
                    result(label,False,'HTTPS probe failed; no certificate bypass attempted')
        else:
            result('public_https',False,'No public origin supplied')
    # A preflight must not certify events it cannot observe.
    for gate in ('human_microphone_acceptance','carrier_live_call','relay_only_media','production_load_and_soak'):
        result(gate,False,'Separate acceptance evidence required; this script cannot certify it')
    return {'status':'PASS' if all(c['status']=='PASS' for c in checks) else 'BLOCKED','checks':checks,'read_only':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--origin')
    parser.add_argument('--output',type=Path,default=Path('docs/verification/voice-deployment-preflight.json'))
    args=parser.parse_args()
    report=asyncio.run(run(args.config,args.origin))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report['status']=='PASS' else 1)

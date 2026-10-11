"""Run all tests on restricted runtime credentials in a disposable database."""
import asyncio
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal

ROOT=Path(__file__).resolve().parents[1]

async def main():
    admin=os.environ.get('AGENT_TEST_ADMIN_URL')
    if not admin:
        admin=json.loads(Path(os.environ['AGENT_TEST_CONFIG']).read_text())['AI_PLATFORM_DATABASE_URL']
    tag='rls_'+uuid4().hex[:12]
    roles={kind:tag+'_'+kind for kind in ('migration','api','worker')}
    passwords={kind:secrets.token_urlsafe(32) for kind in roles}
    urls={kind:make_conninfo(admin,dbname=tag,user=role,password=passwords[kind],options='-c agent.worker='+('off' if kind=='api' else 'on')) for kind,role in roles.items()}
    async with await psycopg.AsyncConnection.connect(admin,autocommit=True) as conn:
        created_groups=[]
        try:
            for group in ('agent_runtime_api','agent_runtime_worker'):
                if not await (await conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(group,))).fetchone():
                    await conn.execute(SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(Identifier(group)))
                    created_groups.append(group)
            for kind,role in roles.items():
                await conn.execute(SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}').format(Identifier(role),Literal(passwords[kind])))
                await conn.execute(SQL('GRANT {} TO {}').format(Identifier('agent_runtime_api' if kind=='api' else 'agent_runtime_worker'),Identifier(role)))
            await conn.execute(SQL('GRANT SET ON PARAMETER "agent.worker" TO {}').format(Identifier(roles['migration'])))
            await conn.execute(SQL('CREATE DATABASE {} OWNER {}').format(Identifier(tag),Identifier(roles['migration'])))
            env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'AI_PLATFORM_DATABASE_URL':urls['migration']}
            await asyncio.to_thread(subprocess.run,[sys.executable,'-m','agent_platform.migrate'],env=env,cwd=ROOT,check=True)
            async with await psycopg.AsyncConnection.connect(urls['migration']) as db:
                await db.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
                await db.execute('GRANT USAGE ON SCHEMA public TO agent_runtime_api,agent_runtime_worker')
                await db.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO agent_runtime_api,agent_runtime_worker')
                await db.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_runtime_api,agent_runtime_worker')
                await db.execute('REVOKE INSERT,UPDATE,DELETE ON agent_schema_migrations FROM agent_runtime_api,agent_runtime_worker')
            env.update(AGENT_TEST_MIGRATION_URL=urls['migration'],AI_PLATFORM_OPERATOR_DATABASE_URL=urls['worker'],AI_PLATFORM_API_DATABASE_URL=urls['api'],AI_PLATFORM_DATABASE_URL=urls['worker'],AGENT_TEST_DATABASE_URL=urls['worker'],AGENT_TEST_API_URL=urls['api'])
            result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','pytest','-q',*(sys.argv[1:] or ['tests/python'])],env=env,cwd=ROOT,check=False)
            return result.returncode
        finally:
            await conn.execute(SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(Identifier(tag)))
            await conn.execute(SQL('REVOKE SET ON PARAMETER "agent.worker" FROM {}').format(Identifier(roles['migration'])))
            for role in roles.values():
                await conn.execute(SQL('DROP ROLE IF EXISTS {}').format(Identifier(role)))
            for group in created_groups:
                await conn.execute(SQL('DROP ROLE {}').format(Identifier(group)))

if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))

"""Provision restricted roles using an operator connection; never print passwords.
Credentials are separate 0600 JSON/environment files; runtime receives one URL.
"""
import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.sql import SQL, Identifier, Literal


async def provision(admin, passwords, *, reuse=False):
    async with await psycopg.AsyncConnection.connect(admin) as conn:
        # Refuse unrelated/shared schemas and existing login roles. Never silently
        # rotate credentials or change ownership of another application's tables.
        names={r[0] for r in await (await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")).fetchall()}
        manifest=json.loads((Path(__file__).resolve().parents[1]/'docs/security/table-inventory.json').read_text())
        if names-{r['name'] for r in manifest['tables']}:
            raise RuntimeError('Unrecognized tables: use a dedicated platform database')
        if not reuse and await (await conn.execute("SELECT 1 FROM pg_roles WHERE rolname=ANY(%s)",(list(passwords),))).fetchone():
            raise RuntimeError('Runtime login roles already exist; use the saved credentials, not automatic rotation')
        for role in ('agent_runtime_api', 'agent_runtime_worker'):
            if not await (await conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,))).fetchone():
                await conn.execute(SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE').format(Identifier(role)))
        for role, password in passwords.items():
            if role not in {'agent_migrator','agent_api','agent_worker'}:
                raise ValueError('Unsupported database role')
            existing=await (await conn.execute('SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=%s', (role,))).fetchone()
            if reuse and (not existing or any(existing)):
                raise RuntimeError('Existing role is missing or privileged')
            if not existing:
                await conn.execute(SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE').format(Identifier(role)))
            if not reuse:
                await conn.execute(SQL('ALTER ROLE {} PASSWORD {}').format(Identifier(role), Literal(password)))
        await conn.execute('GRANT SET ON PARAMETER "agent.worker" TO agent_migrator')
        await conn.execute('GRANT agent_runtime_api TO agent_api')
        await conn.execute('GRANT agent_runtime_worker TO agent_worker,agent_migrator')
        await conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        await conn.execute('GRANT USAGE ON SCHEMA public TO agent_runtime_api,agent_runtime_worker')
        await conn.execute('GRANT USAGE,CREATE ON SCHEMA public TO agent_migrator')
        # Migrations own existing platform objects; database itself stays operator-owned.
        tables=await (await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")).fetchall()
        for (table,) in tables:
            await conn.execute(SQL('ALTER TABLE {} OWNER TO agent_migrator').format(Identifier(table)))
        await conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO agent_runtime_api,agent_runtime_worker')
        if 'agent_schema_migrations' in names:
            await conn.execute('REVOKE INSERT,UPDATE,DELETE ON agent_schema_migrations FROM agent_runtime_api,agent_runtime_worker')
        await conn.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO agent_runtime_api,agent_runtime_worker')
        await conn.execute('ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO agent_runtime_api,agent_runtime_worker')
        await conn.execute('ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO agent_runtime_api,agent_runtime_worker')
        await conn.execute('ALTER ROLE agent_worker SET "agent.worker" TO \'on\'')
        await conn.execute('ALTER ROLE agent_migrator SET "agent.worker" TO \'on\'')


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reuse-roles',type=Path,help='Reuse private credentials for a second platform database on the same cluster')
    args=parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output exists; preserve existing credentials and use explicit rotation')
    admin=os.environ['AGENT_DB_ADMIN_URL']
    passwords={role:secrets.token_urlsafe(40) for role in ('agent_migrator','agent_api','agent_worker')}
    if args.reuse_roles:
        if args.reuse_roles.is_symlink() or args.reuse_roles.stat().st_mode & 0o077:
            raise SystemExit('Existing role file must be private')
        previous=json.loads(args.reuse_roles.read_text())
        admin_parts=conninfo_to_dict(admin)
        for role in passwords:
            parts=conninfo_to_dict(previous[role])
            if parts.get('user')!=role or any(parts.get(k)!=admin_parts.get(k) for k in ('host','port')):
                raise SystemExit('Reuse is limited to the same cluster and expected roles')
            passwords[role]=parts['password']
    result={role:make_conninfo(admin,user=role,password=password) for role,password in passwords.items()}
    fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(result,stream)
    await provision(admin,passwords,reuse=bool(args.reuse_roles))
    print('Restricted database credentials saved privately.')


if __name__=='__main__':
    asyncio.run(main())

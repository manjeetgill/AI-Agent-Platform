"""Provision a separate runtime role for an already migrated database.

Run with an operator DB URL. Writes a private configuration; never prints the
password. This is DB least privilege, not row-level tenant isolation.
"""
import argparse
import asyncio
import json
import os
import re
import secrets
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.sql import SQL, Identifier, Literal

PROTECTED={'agent_schema_migrations','voice_rate_cards','voice_spend_limits'}


async def provision(config, output, name):
    if not re.fullmatch(r'voice_runtime_[a-z0-9_]{1,32}',name):
        raise ValueError('Role name must start voice_runtime_ and contain lowercase letters/digits/underscores')
    if config.stat().st_mode & 0o077:
        raise ValueError('Operator config must be private (0600)')
    values=json.loads(config.read_text())
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; never overwrite credentials')
    password=secrets.token_urlsafe(48)
    url=values['AI_PLATFORM_DATABASE_URL']
    async with await psycopg.AsyncConnection.connect(url) as conn, conn.transaction():
        if await (await conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,))).fetchone():
            raise ValueError('Role already exists; no credentials changed')
        await conn.execute(SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}').format(Identifier(name),Literal(password)))
        await conn.execute(SQL('GRANT CONNECT ON DATABASE {} TO {}').format(Identifier(conn.info.dbname),Identifier(name)))
        await conn.execute(SQL('GRANT USAGE ON SCHEMA public TO {}').format(Identifier(name)))
        tables=await (await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")).fetchall()
        for (table,) in tables:
            permissions=SQL('SELECT') if table in PROTECTED else SQL('SELECT,INSERT,UPDATE,DELETE')
            await conn.execute(SQL('GRANT {} ON TABLE public.{} TO {}').format(permissions,Identifier(table),Identifier(name)))
        await conn.execute(SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(Identifier(name)))
        # PostgreSQL 15+ databases normally have this setting; refuse unsafe older grants.
        creates=await (await conn.execute("SELECT has_schema_privilege(%s,'public','CREATE')",(name,))).fetchone()
        if creates[0]:
            raise ValueError('PUBLIC has schema CREATE; operator must remove that inherited grant before using this role')
        runtime=conninfo_to_dict(url)
        runtime.update(user=name,password=password)
        values['AI_PLATFORM_DATABASE_URL']=make_conninfo(**runtime)
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as file:
            json.dump(values,file,indent=2)
    print('Runtime role created; private configuration saved. Restart is a separate drained operation.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--role',required=True)
    args=parser.parse_args()
    asyncio.run(provision(args.config,args.output,args.role))

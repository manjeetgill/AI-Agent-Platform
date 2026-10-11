"""Operator-provisioned service credentials; end-user mappings belong to an authenticated app."""
import argparse
import asyncio
import hashlib
import hmac
import os
import secrets
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCOPES = {'services:read', 'services:write', 'notifications:read', 'notifications:send', 'memory:read', 'memory:write', 'memory:review', 'calling:read', 'calling:write',
          'telephony:read', 'telephony:write', 'channels:read', 'channels:write',
          'tasks:read', 'tasks:write', 'tasks:approve', 'agents:read', 'agents:write',
          'agents:run', 'knowledge:read', 'knowledge:write', 'knowledge:publish', 'alerts:send'}


async def provision(pool, *, tenant, organisation, app_name, scopes, days=30, app_id=None, connection=None):
    """Trusted operator entry point. Return a credential once; persist only its digest."""
    if not scopes or not set(scopes) <= SCOPES or not 1 <= days <= 90:
        raise ValueError('Select supported scopes and a lifetime of 1–90 days')
    if not tenant or not organisation or not app_name:
        raise ValueError('Organisation and application names are required')
    key_id, secret = uuid4(), secrets.token_urlsafe(32)
    async with (pool.connection() if connection is None else nullcontext(connection)) as conn, conn.transaction():
        if app_id:
            app = await (await conn.execute('SELECT id FROM platform_apps WHERE id=%s AND tenant_id=%s',
                                           (app_id, tenant))).fetchone()
            if not app:
                raise ValueError('Application does not belong to this organisation')
        else:
            await conn.execute('INSERT INTO platform_organisations(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                               (tenant, organisation))
            app_id = uuid4()
            await conn.execute('INSERT INTO platform_apps(id,tenant_id,name) VALUES(%s,%s,%s)',
                               (app_id, tenant, app_name))
        await conn.execute('''INSERT INTO platform_service_keys(id,app_id,secret_hash,scopes,expires_at)
            VALUES(%s,%s,%s,%s,%s)''', (key_id, app_id, hashlib.sha256(secret.encode()).hexdigest(),
            sorted(set(scopes)), datetime.now(UTC) + timedelta(days=days)))
    return {'app_id': str(app_id), 'key_id': str(key_id), 'credential': f'apk_{key_id}.{secret}'}


async def authenticate(pool, authorization, external_user, scope):
    invalid = HTTPException(401, 'Invalid or expired service credential')
    try:
        key_id, secret = authorization.removeprefix('Bearer apk_').split('.')
        key_id = UUID(key_id)
        if not 32 <= len(secret) <= 128:
            raise ValueError()
    except (ValueError, AttributeError):
        raise invalid from None
    from .db_scope import APIPool, identity
    if isinstance(pool, APIPool):
        if not external_user or len(external_user) > 200 or any(ord(c) < 32 for c in external_user):
            raise HTTPException(422, 'X-External-User-ID is required (1–200 characters)')
        async with pool.connection() as conn:
            row = await (await conn.execute('SELECT agent_authenticate_service(%s,%s,%s) AS claims',
                (key_id, hashlib.sha256(secret.encode()).hexdigest(), external_user))).fetchone()
        claims = row['claims'] if row else None
        if not claims:
            raise invalid
        if scope is not None and scope not in claims['scopes']:
            raise HTTPException(403, 'Service credential lacks the required scope')
        identity.set(claims)
        return claims
    async with pool.connection() as conn, conn.transaction():
        row = await (await conn.execute('''SELECT k.*,a.tenant_id FROM platform_service_keys k
            JOIN platform_apps a ON a.id=k.app_id WHERE k.id=%s AND k.revoked_at IS NULL
            AND k.expires_at>now()''', (key_id,))).fetchone()
        if not row or not hmac.compare_digest(row['secret_hash'], hashlib.sha256(secret.encode()).hexdigest()):
            raise invalid
        if scope is not None and scope not in row['scopes']:
            raise HTTPException(403, 'Service credential lacks the required scope')
        if not external_user or len(external_user) > 200 or any(ord(c) < 32 for c in external_user):
            raise HTTPException(422, 'X-External-User-ID is required (1–200 characters)')
        owner = await (await conn.execute('''SELECT owner_id FROM platform_external_users
            WHERE app_id=%s AND external_id=%s''', (row['app_id'], external_user))).fetchone()
        if owner is None:
            # Existing identities are read-only. A concurrent first request may win
            # the insert; a new SELECT observes that winner under READ COMMITTED.
            await conn.execute('''INSERT INTO platform_external_users(app_id,external_id,owner_id)
                VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''', (row['app_id'], external_user, uuid4()))
            owner = await (await conn.execute('''SELECT owner_id FROM platform_external_users
                WHERE app_id=%s AND external_id=%s''', (row['app_id'], external_user))).fetchone()
        return {'tenant': row['tenant_id'], 'user': str(owner['owner_id']),
                'principal': 'service:' + str(key_id), 'app_id': str(row['app_id']),
                'scopes': row['scopes']}


async def main():
    parser = argparse.ArgumentParser(description='Operator-only service credential administration')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    for name in ('tenant', 'organisation', 'app-name'):
        create.add_argument('--' + name, required=True)
    create.add_argument('--scope', action='append', choices=sorted(SCOPES), required=True)
    create.add_argument('--days', type=int, default=30)
    create.add_argument('--app-id', type=UUID, help='Rotate a key while preserving app/user identity')
    revoke = sub.add_parser('revoke')
    revoke.add_argument('key_id', type=UUID)
    for command in ('grant-tool', 'revoke-tool'):
        tool = sub.add_parser(command)
        tool.add_argument('app_id', type=UUID)
        tool.add_argument('tool')
    args = parser.parse_args()
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], open=False,
                                   kwargs={'row_factory': dict_row}) as pool:
        from .db_scope import assert_runtime_role
        await assert_runtime_role(pool,'worker')
        if args.command in {'grant-tool', 'revoke-tool'}:
            async with pool.connection() as conn:
                if args.command == 'grant-tool':
                    await conn.execute('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                                       (args.app_id, args.tool))
                else:
                    await conn.execute('DELETE FROM platform_tool_grants WHERE app_id=%s AND tool=%s',
                                       (args.app_id, args.tool))
        elif args.command == 'revoke':
            async with pool.connection() as conn:
                await conn.execute('UPDATE platform_service_keys SET revoked_at=now() WHERE id=%s', (args.key_id,))
        else:
            import json
            print(json.dumps(await provision(pool, tenant=args.tenant, organisation=args.organisation,
                app_name=args.app_name, scopes=args.scope, days=args.days, app_id=args.app_id)))


if __name__ == '__main__':
    asyncio.run(main())

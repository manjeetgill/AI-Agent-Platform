import asyncio
import base64
import os
import time
from uuid import uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_durable_tasks import database

from agent_platform.db_scope import APIPool, identity
from agent_platform.delegation import authenticate as delegation_authenticate
from agent_platform.delegation import issue, verify_v2
from agent_platform.service_identity import authenticate, provision
from agent_platform.tokens import TokenError


def api_database():
    if not os.environ.get('AGENT_TEST_API_URL'):
        pytest.skip('Run scripts/test_restricted.py for separate API/worker roles')
    return os.environ['AGENT_TEST_API_URL']


def test_every_tenant_table_fails_closed_without_scope():
    with psycopg.connect(api_database(),row_factory=dict_row) as conn:
        flags=conn.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        assert not flags['rolsuper'] and not flags['rolbypassrls']
        tables=conn.execute("SELECT tablename,rowsecurity FROM pg_tables WHERE schemaname='public'").fetchall()
        global_tables={'agent_schema_migrations','platform_worker_heartbeats','whatsapp_worker_health',
                       'whatsapp_call_worker_health','meta_webhook_receipts','meta_platform_webhook'}
        for table in tables:
            if table['tablename'] in global_tables:
                continue
            assert table['rowsecurity'],table['tablename']
            for worker in ('off','on'):
                conn.execute("SELECT set_config('agent.worker',%s,true)",(worker,))
                assert conn.execute(psycopg.sql.SQL('SELECT count(*) AS n FROM {}').format(psycopg.sql.Identifier(table['tablename']))).fetchone()['n']==0,table['tablename']


def test_scoped_api_identity_and_cross_tenant_tasks():
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as worker:
            a=await provision(worker,tenant='rls-a-'+uuid4().hex,organisation='A',app_name='A',scopes=['tasks:read'])
            b=await provision(worker,tenant='rls-b-'+uuid4().hex,organisation='B',app_name='B',scopes=['tasks:read'])
            ac=await authenticate(worker,'Bearer '+a['credential'],'one','tasks:read')
            bc=await authenticate(worker,'Bearer '+b['credential'],'one','tasks:read')
            # Schema-level fixtures avoid depending on planner/model availability.
            async with worker.connection() as conn:
                for c in (ac,bc):
                    await conn.execute("INSERT INTO chore_tasks(id,tenant_id,owner_id,source_app_id,request,status,request_key) VALUES(%s,%s,%s,%s,%s,'DRAFT',%s)",
                                       (uuid4(),c['tenant'],c['user'],c['app_id'],'read only',uuid4().hex))
        async with APIPool(api_database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            try:
                claims=await authenticate(pool,'Bearer '+a['credential'],'one','tasks:read')
                assert claims['user']==ac['user']
                async with pool.connection() as conn:
                    rows=await (await conn.execute('SELECT tenant_id FROM chore_tasks')).fetchall()
                    assert rows and {r['tenant_id'] for r in rows}=={ac['tenant']}
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        async with conn.transaction():
                            await conn.execute("INSERT INTO chore_tasks(id,tenant_id,owner_id,source_app_id,request,status,request_key) VALUES(%s,%s,%s,%s,%s,'DRAFT',%s)",
                                               (uuid4(),bc['tenant'],bc['user'],bc['app_id'],'wrong tenant',uuid4().hex))
                with pytest.raises(HTTPException):
                    await authenticate(pool,'Bearer '+a['credential'][:-1]+'!', 'one','tasks:read')
            finally:
                identity.set(None)
    asyncio.run(run())


def test_v2_rotation_binding_and_replay(monkeypatch):
    import json
    private=Ed25519PrivateKey.generate()
    private_bytes=private.private_bytes_raw()
    now=int(time.time())
    tenant='delegation-'+uuid4().hex
    async def setup_app():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as worker:
            return await provision(worker,tenant=tenant,organisation='Delegation',app_name='Delegation',scopes=['tasks:read'])
    app=asyncio.run(setup_app())
    key={'issuer':'test-app','kid':'current','tenant':tenant,'app_id':app['app_id'],
         'public_key':base64.b64encode(private.public_key().public_bytes_raw()).decode(),
         'not_before':now-10,'not_after':now+600}
    old={**key,'kid':'old','not_after':now-1}
    monkeypatch.setenv('AI_DELEGATION_ISSUER_KEYS',json.dumps([old,key]))
    token=issue(private_bytes,issuer=key['issuer'],kid='current',tenant=tenant,app_id=key['app_id'],user=str(uuid4()))
    assert verify_v2(token)['tenant']==tenant
    for changed in ({'kid':'old'},{'tenant':'another'},{'app_id':str(uuid4())}):
        args={'issuer':key['issuer'],'kid':'current','tenant':tenant,'app_id':key['app_id'],'user':str(uuid4()),**changed}
        with pytest.raises(TokenError):
            verify_v2(issue(private_bytes,**args))
    async def run():
        async with APIPool(api_database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            await delegation_authenticate(pool,'',token)
            with pytest.raises(TokenError,match='already used'):
                await delegation_authenticate(pool,'',token)
    asyncio.run(run())


def test_app_scope_cannot_be_combined_with_wrong_tenant():
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as worker:
            app=await provision(worker,tenant='integrity-'+uuid4().hex,organisation='Test',app_name='Test',scopes=['tasks:read'])
        with psycopg.connect(api_database(),row_factory=dict_row) as conn:
            conn.execute("SELECT set_config('agent.tenant','wrong-tenant',true),set_config('agent.app',%s,true),set_config('agent.owner','wrong-owner',true)",(app['app_id'],))
            tables=conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND rowsecurity").fetchall()
            for table in tables:
                assert conn.execute(psycopg.sql.SQL('SELECT count(*) AS n FROM {}').format(psycopg.sql.Identifier(table['tablename']))).fetchone()['n']==0,table['tablename']
    asyncio.run(run())


def test_service_key_cannot_inherit_legacy_research_authority():
    from fastapi.testclient import TestClient

    from agent_platform.app import create_app
    async def setup():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as worker:
            return await provision(worker,tenant='nraialgo',organisation='Test',app_name='Limited',scopes=['tasks:read'])
    key=asyncio.run(setup())
    with TestClient(create_app(signing_key='s'*48)) as client:
        response=client.get('/v1/agents/catalog',headers={'Authorization':'Bearer '+key['credential'],'X-External-User-ID':'user'})
        assert response.status_code==403


def test_private_issuer_file_round_trip_and_permissions(tmp_path):
    import json

    from agent_platform.delegation import issue_from_file
    private=Ed25519PrivateKey.generate()
    now=int(time.time())
    public={'issuer':'app','kid':'key','tenant':'tenant','app_id':str(uuid4()),
            'public_key':base64.b64encode(private.public_key().public_bytes_raw()).decode(),
            'not_before':now-1,'not_after':now+600}
    path=tmp_path/'issuer.json'
    path.write_text(json.dumps({**public,'private_key':base64.b64encode(private.private_bytes_raw()).decode()}))
    path.chmod(0o600)
    first=issue_from_file(path,user='owner')
    second=issue_from_file(path,user='owner')
    assert verify_v2(first,keys=[public])['jti']!=verify_v2(second,keys=[public])['jti']
    path.chmod(0o644)
    with pytest.raises(TokenError,match='private'):
        issue_from_file(path,user='owner')

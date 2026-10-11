import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from test_durable_tasks import database

from agent_platform.gateway import Gateway, NVIDIAProvider
from agent_platform.provider_chain import Entry, reserve
from agent_platform.runtime_errors import RunFailure
from agent_platform.service_identity import authenticate, provision


class Reply(BaseModel):
    answer:str


def test_chain_exhaustion_cooldown_recovery_and_isolation(monkeypatch):
    config=[{'provider':p,'model':'test','key_env':env,'daily_budget_microusd':1000000,
             'microusd_per_token':1,'sensitivity':['public'],'cooldown_seconds':60}
            for p,env in [('nvidia','TEST_NVIDIA_KEY'),('gemini','TEST_GEMINI_KEY')]]
    monkeypatch.setenv('AI_MODEL_PROVIDER_CHAIN',json.dumps(config))
    monkeypatch.setenv('TEST_NVIDIA_KEY','test-nvidia')
    monkeypatch.setenv('TEST_GEMINI_KEY','test-gemini')
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_FAST_MODEL','test')
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            creds=await provision(pool,tenant='chain-'+uuid4().hex,organisation='Test',app_name='Test',scopes=['tasks:read'])
            claims=await authenticate(pool,'Bearer '+creds['credential'],'user','tasks:read')
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO platform_model_policies(app_id,daily_budget_microusd,daily_calls,allowed_providers,sensitivity) VALUES(%s,100000,5,ARRAY['nvidia','gemini'],'public')",(claims['app_id'],))
            seen=[]
            throttled=True
            def handler(request):
                seen.append(request.url.host)
                if throttled and request.url.host=='integrate.api.nvidia.com':
                    return httpx.Response(429)
                return httpx.Response(200,json={'choices':[{'message':{'content':'{"answer":"ok"}'}}],'usage':{'total_tokens':10}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                gateway=Gateway(NVIDIAProvider('test'),client=client,pool=pool)
                result=await gateway.structured('research','policy',{},Reply,lambda x:x,claims=claims)
                assert result[0].answer=='ok' and result[2]=='gemini'
                seen.clear()
                await gateway.structured('research','policy',{},Reply,lambda x:x,claims=claims)
                assert seen==['generativelanguage.googleapis.com']
                async with pool.connection() as conn:
                    await conn.execute('UPDATE provider_chain_usage SET cooldown_until=now()-interval \'1 second\' WHERE app_id=%s',(claims['app_id'],))
                throttled=False
                assert (await gateway.structured('research','policy',{},Reply,lambda x:x,claims=claims))[2]=='nvidia'
                async with pool.connection() as conn:
                    await conn.execute('UPDATE platform_model_policies SET daily_calls=0 WHERE app_id=%s',(claims['app_id'],))
                seen.clear()
                with pytest.raises(RunFailure,match='MODEL_CHAIN_EXHAUSTED'):
                    await gateway.structured('research','policy',{},Reply,lambda x:x,claims=claims)
                assert not seen
    asyncio.run(run())


def test_concurrent_budget_reservations_do_not_overspend():
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            creds=await provision(pool,tenant='budget-'+uuid4().hex,organisation='Test',app_name='Test',scopes=['tasks:read'])
            claims=await authenticate(pool,'Bearer '+creds['credential'],'user','tasks:read')
            provider='test-'+uuid4().hex
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO platform_model_policies(app_id,daily_budget_microusd,daily_calls,allowed_providers,sensitivity) VALUES(%s,100,100,%s,'restricted')",(claims['app_id'],[provider]))
            entry=Entry(provider=provider,model='test',key_env='TEST_KEY',daily_budget_microusd=100,microusd_per_token=1,sensitivity=['restricted'])
            results=await asyncio.gather(*(reserve(pool,claims,entry,60) for _ in range(8)))
            assert sum(x is not None for x in results)==1
            entry.sensitivity=['public']
            assert await reserve(pool,claims,entry,1) is None
    asyncio.run(run())


def test_unknown_usage_retains_reservation_and_daily_usage_resets():
    from agent_platform.provider_chain import finish
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            creds=await provision(pool,tenant='reset-'+uuid4().hex,organisation='Test',app_name='Test',scopes=['tasks:read'])
            claims=await authenticate(pool,'Bearer '+creds['credential'],'user','tasks:read')
            provider='test-'+uuid4().hex
            async with pool.connection() as conn:
                await conn.execute("INSERT INTO platform_model_policies(app_id,daily_budget_microusd,daily_calls,allowed_providers,sensitivity) VALUES(%s,100,2,%s,'public')",(claims['app_id'],[provider]))
            entry=Entry(provider=provider,model='test',key_env='TEST_KEY',daily_budget_microusd=100,microusd_per_token=1,sensitivity=['public'])
            first=await reserve(pool,claims,entry,80)
            await finish(pool,claims,first,entry,tokens=0,status='COMPLETED')
            assert await reserve(pool,claims,entry,30) is None
            async with pool.connection() as conn:
                await conn.execute("UPDATE provider_chain_usage SET day=day-1 WHERE app_id=%s",(claims['app_id'],))
            assert await reserve(pool,claims,entry,80) is not None
    asyncio.run(run())


def test_provider_cap_is_shared_across_apps():
    async def run():
        async with AsyncConnectionPool(database(),open=False,kwargs={'row_factory':dict_row}) as pool:
            provider='shared-'+uuid4().hex
            entry=Entry(provider=provider,model='test',key_env='TEST_KEY',daily_budget_microusd=100,microusd_per_token=1,sensitivity=['public'])
            identities=[]
            for _ in range(2):
                key=await provision(pool,tenant='shared-budget-'+uuid4().hex,organisation='Test',app_name='Test',scopes=['tasks:read'])
                claims=await authenticate(pool,'Bearer '+key['credential'],'user','tasks:read')
                identities.append(claims)
                async with pool.connection() as conn:
                    await conn.execute("INSERT INTO platform_model_policies VALUES(%s,100,10,%s,'public')",(claims['app_id'],[provider]))
            results=await asyncio.gather(*(reserve(pool,c,entry,60) for c in identities))
            assert sum(r is not None for r in results)==1
    asyncio.run(run())

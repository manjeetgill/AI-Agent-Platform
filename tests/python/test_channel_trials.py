import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform.channel_trials import reserve_committed


def test_trial_atomic_limits_expiry_and_no_uncertain_refunds(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        headers, _, _ = setup(client, key, monkeypatch)
        app = key['app_id']
        sql("INSERT INTO channel_trial_policies VALUES(%s,now()+interval '1 day',2,1,1)", (app,))
        async def compete():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory':dict_row}) as pool:
                return await asyncio.gather(*(reserve_committed(pool,app,'whatsapp_turns',uuid4()) for _ in range(8)))
        assert sum(asyncio.run(compete())) == 2
        operation = uuid4()
        async def once(resource, op):
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory':dict_row}) as pool:
                return await reserve_committed(pool,app,resource,op)
        assert asyncio.run(once('whatsapp_sends',operation))
        assert not asyncio.run(once('whatsapp_sends',operation))
        assert not asyncio.run(once('whatsapp_sends',uuid4()))
        status = client.get('/v1/whatsapp/usage',headers=headers)
        assert status.status_code == 200
        assert status.json()['monetary_budget'] is False
        assert {r['resource']:r['units'] for r in status.json()['usage_today']} == {'whatsapp_turns':2,'whatsapp_sends':1}
        sql("UPDATE channel_trial_policies SET expires_at=now()-interval '1 second' WHERE app_id=%s", (app,))
        assert not asyncio.run(once('voice_calls',uuid4()))
        assert client.get('/v1/whatsapp/usage').status_code == 401

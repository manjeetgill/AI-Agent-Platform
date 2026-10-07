"""Cross-app alert routing, consent, idempotency and uncertain-send fences."""
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent_platform.service_identity import provision
from agent_platform.whatsapp_alerts import tick_alerts
from agent_platform.whatsapp import ingest
from test_task_agents import database, environment, sql
from test_whatsapp import setup


def test_shared_sender_alert_is_scoped_and_sent_once(monkeypatch):
    with environment(monkeypatch) as (client, _, owner_key, _):
        _, channel, doc = setup(client, owner_key, monkeypatch)
        tenant = sql('SELECT tenant_id FROM platform_apps WHERE id=%s', (owner_key['app_id'],))[0]['tenant_id']

        async def make_key():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                return await provision(pool, tenant=tenant, organisation='Fixture', app_name='Market alerts',
                                       scopes=['alerts:send'])

        app_key = asyncio.run(make_key())
        headers = {'Authorization': 'Bearer ' + app_key['credential'], 'X-External-User-ID': 'investor-1'}
        path = '/v1/whatsapp/alerts'
        recipient = '919876543210'
        sql('INSERT INTO whatsapp_alert_routes(app_id,channel_id,enabled) VALUES(%s,%s,true)',
            (app_key['app_id'], channel['id']))
        conversation = uuid4()
        sql("INSERT INTO whatsapp_conversations(id,channel_id,customer) VALUES(%s,%s,%s)",
            (conversation, channel['id'], recipient))
        sql("""INSERT INTO whatsapp_messages(id,conversation_id,provider_id,body,received_at,status)
            VALUES(%s,%s,%s,'I want alerts',now(),'SENT')""", (uuid4(), conversation, uuid4().hex))
        calls = []

        async def fake_send(config, phone, customer, body):
            calls.append((customer, body))
            return 'wamid.alert-one'

        try:
            assert client.put(path+'/subscription', headers=headers,
                              json={'recipient': recipient, 'consented': False}).status_code == 422
            assert client.put(path+'/subscription', headers={**headers, 'X-External-User-ID': 'investor-2'},
                              json={'recipient': '919999999999', 'consented': True}).status_code == 409
            assert client.put(path+'/subscription', headers=headers,
                              json={'recipient': recipient, 'consented': True}).status_code == 200
            body = {'request_key': 'market-alert-123', 'exchange': 'NSE', 'symbol': 'NIFTY',
                    'timeframe': 'live', 'condition': 'price_above', 'target': '25000',
                    'observed': '25001', 'triggered_at': datetime.now(UTC).isoformat()}
            first = client.post(path+'/deliveries', headers=headers, json=body)
            assert first.status_code == 202, first.text
            assert client.post(path+'/deliveries', headers=headers, json=body).json()['id'] == first.json()['id']
            assert client.post(path+'/deliveries', headers=headers,
                               json={**body, 'observed': '25002'}).status_code == 409
            assert client.get(path+'/deliveries/'+first.json()['id'], headers={**headers,
                              'X-External-User-ID': 'investor-2'}).status_code == 404

            async def work():
                async with AsyncConnectionPool(database(), open=False,
                                               kwargs={'row_factory': dict_row}) as pool:
                    assert await tick_alerts(pool, sender=fake_send)
                    assert not await tick_alerts(pool, sender=fake_send)
            asyncio.run(work())
            assert len(calls) == 1
            assert calls[0][0] == recipient
            assert 'NSE:NIFTY' in calls[0][1] and '₹25001' in calls[0][1]
            assert client.get(path+'/deliveries/'+first.json()['id'], headers=headers).json()['status'] == 'ACCEPTED'
            async def receipt():
                async with AsyncConnectionPool(database(), open=False,
                                               kwargs={'row_factory': dict_row}) as pool:
                    await ingest(pool, channel, {'entry': [{'changes': [{'value': {
                        'metadata': {'phone_number_id': channel['phone_id']},
                        'statuses': [{'id': 'wamid.alert-one', 'status': 'delivered'}],
                    }}]}]})
            asyncio.run(receipt())
            assert client.get(path+'/deliveries/'+first.json()['id'], headers=headers).json()['delivery'] == 'delivered'
        finally:
            sql('DELETE FROM platform_external_users WHERE app_id=%s', (app_key['app_id'],))
            sql('DELETE FROM platform_service_keys WHERE app_id=%s', (app_key['app_id'],))
            sql('DELETE FROM platform_apps WHERE id=%s', (app_key['app_id'],))
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))

"""Cross-app alert routing, consent, idempotency and uncertain-send fences."""
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform.service_identity import provision
from agent_platform.whatsapp import ingest
from agent_platform.whatsapp_alerts import tick_alerts


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
        sql('''INSERT INTO whatsapp_alert_routes
            (app_id,channel_id,enabled,require_recipient_verification)
            VALUES(%s,%s,true,false)''',
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


def test_public_recipient_must_verify_from_claimed_whatsapp_number(monkeypatch):
    with environment(monkeypatch) as (client, _, owner_key, _):
        _, channel, doc = setup(client, owner_key, monkeypatch)
        tenant = sql('SELECT tenant_id FROM platform_apps WHERE id=%s', (owner_key['app_id'],))[0]['tenant_id']

        async def make_key():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                return await provision(pool, tenant=tenant, organisation='Fixture', app_name='Public alerts',
                                       scopes=['alerts:send'])

        app_key = asyncio.run(make_key())
        headers = {'Authorization': 'Bearer ' + app_key['credential'], 'X-External-User-ID': 'public-user'}
        other = {**headers, 'X-External-User-ID': 'different-user'}
        recipient = '919999000001'
        path = '/v1/whatsapp/alerts'
        sql('''INSERT INTO whatsapp_alert_routes(app_id,channel_id,sender_number,enabled)
            VALUES(%s,%s,'+15556377356',true)''', (app_key['app_id'], channel['id']))
        try:
            created = client.post(path + '/verifications', headers=headers, json={'recipient': '+' + recipient})
            assert created.status_code == 201, created.text
            challenge = created.json()
            assert challenge['message'].startswith('VERIFY ')
            assert challenge['sender_number'] == '+15556377356'
            stored = sql('SELECT code_hash FROM whatsapp_alert_verifications WHERE id=%s',
                         (challenge['id'],))[0]['code_hash']
            assert len(stored) == 64 and challenge['message'] not in stored
            assert client.put(path + '/subscription', headers=headers,
                              json={'recipient': recipient, 'consented': True}).status_code == 409
            assert client.get(path + '/verifications/' + challenge['id'], headers=other).status_code == 404

            async def verify():
                async with AsyncConnectionPool(database(), open=False,
                                               kwargs={'row_factory': dict_row}) as pool:
                    await ingest(pool, channel, {'entry': [{'changes': [{'value': {
                        'metadata': {'phone_number_id': channel['phone_id']},
                        'messages': [{'id': 'wamid.verify-one', 'from': recipient,
                                      'timestamp': str(int(datetime.now(UTC).timestamp())),
                                      'type': 'text', 'text': {'body': challenge['message']}}],
                    }}]}]})
            asyncio.run(verify())
            assert client.get(path + '/verifications/' + challenge['id'], headers=headers).json()['state'] == 'VERIFIED'
            message_row = sql("SELECT status FROM whatsapp_messages WHERE provider_id='wamid.verify-one'")[0]
            assert message_row['status'] == 'CANCELLED'
            subscribed = client.put(path + '/subscription', headers=headers,
                                    json={'recipient': recipient, 'consented': True})
            assert subscribed.status_code == 200, subscribed.text
            assert client.get(path + '/verifications/' + challenge['id'], headers=headers).json()['state'] == 'CONSUMED'
            # Repeating the same active subscription is safe and does not need a new challenge.
            assert client.put(path + '/subscription', headers=headers,
                              json={'recipient': recipient, 'consented': True}).status_code == 200
        finally:
            sql('DELETE FROM platform_external_users WHERE app_id=%s', (app_key['app_id'],))
            sql('DELETE FROM platform_service_keys WHERE app_id=%s', (app_key['app_id'],))
            sql('DELETE FROM platform_apps WHERE id=%s', (app_key['app_id'],))
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))

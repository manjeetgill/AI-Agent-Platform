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
from agent_platform.whatsapp_alerts import (
    NotificationDelivery,
    RouteConfiguration,
    notification_message,
    tick_alerts,
)


def test_route_configuration_can_disable_application_daily_limit():
    assert RouteConfiguration(channel_id=uuid4(), daily_limit=None).daily_limit is None


def test_generic_notification_is_bounded_and_provider_neutral():
    payload = NotificationDelivery.model_validate({
        'request_key': 'event-12345678', 'category': 'booking.confirmed',
        'title': ' Appointment\nconfirmed ', 'summary': 'Tomorrow\r\nat 10:30 AM',
        'reference': ' APT-123 ', 'occurred_at': datetime(2026, 10, 8, 5, 0, tzinfo=UTC),
        'test_mode': True,
    })
    rendered = notification_message(' Clinic\nApp ', payload)
    assert rendered.startswith('Clinic App TEST notification\nAppointment confirmed')
    assert 'Tomorrow at 10:30 AM' in rendered
    assert 'This message cannot make a payment or complete a transaction.' in rendered


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
        # A different signed-in app user reaches verification through the same sender.
        assert client.post(path + '/verifications', headers=other,
                           json={'recipient': '+' + recipient}).status_code == 201
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
            # Recover a valid provider-persisted message if the webhook left its
            # challenge pending; another app user must still be unable to read it.
            recovery = client.post(path + '/verifications', headers=other,
                                   json={'recipient': recipient}).json()
            conversation = sql('SELECT id FROM whatsapp_conversations WHERE channel_id=%s AND customer=%s',
                               (channel['id'], recipient))[0]['id']
            sql('''INSERT INTO whatsapp_messages(id,conversation_id,provider_id,body,received_at,status)
                VALUES(%s,%s,%s,%s,now(),'PENDING')''',
                (uuid4(), conversation, 'wamid.persisted-verification', recovery['message']))
            assert client.get(path + '/verifications/' + recovery['id'], headers=headers).status_code == 404
            assert client.get(path + '/verifications/' + recovery['id'], headers=other).json()['state'] == 'VERIFIED'
        finally:
            sql('DELETE FROM platform_external_users WHERE app_id=%s', (app_key['app_id'],))
            sql('DELETE FROM platform_service_keys WHERE app_id=%s', (app_key['app_id'],))
            sql('DELETE FROM platform_apps WHERE id=%s', (app_key['app_id'],))
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))


def test_owned_sender_route_and_generic_notification(monkeypatch):
    with environment(monkeypatch) as (client, _, owner_key, _):
        _, channel, doc = setup(client, owner_key, monkeypatch)
        tenant = sql('SELECT tenant_id FROM platform_apps WHERE id=%s', (owner_key['app_id'],))[0]['tenant_id']

        async def make_key():
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                return await provision(pool, tenant=tenant, organisation='Fixture', app_name='Fixture',
                                       app_id=owner_key['app_id'],
                                       scopes=['channels:read', 'channels:write', 'alerts:send'])

        key = asyncio.run(make_key())
        headers = {'Authorization': 'Bearer ' + key['credential'], 'X-External-User-ID': 'operator'}
        recipient = '919999000001'
        path = '/v1/whatsapp/alerts'

        async def inbound(body, ident):
            async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                await ingest(pool, channel, {'entry': [{'changes': [{'value': {
                    'metadata': {'phone_number_id': channel['phone_id'],
                                 'display_phone_number': '+1 (555) 637-7356'},
                    'messages': [{'id': ident, 'from': recipient,
                                  'timestamp': str(int(datetime.now(UTC).timestamp())),
                                  'type': 'text', 'text': {'body': body}}],
                }}]}]})

        try:
            asyncio.run(inbound('hello', 'wamid.route-display'))
            configured = client.put(path + '/route', headers=headers,
                                    json={'channel_id': channel['id'], 'daily_limit': 3})
            assert configured.status_code == 200, configured.text
            assert configured.json()['sender_number'] == '+15556377356'
            assert client.get(path + '/route', headers=headers).json()['daily_limit'] == 3
            challenge = client.post(path + '/verifications', headers=headers,
                                    json={'recipient': recipient}).json()
            asyncio.run(inbound(challenge['message'], 'wamid.route-verify'))
            assert client.put(path + '/subscription', headers=headers,
                              json={'recipient': recipient, 'consented': True}).status_code == 200
            payload = {'request_key': 'booking-confirmed-123', 'category': 'booking.confirmed',
                       'title': 'Appointment confirmed',
                       'summary': 'Your appointment is tomorrow at 10:30 AM.',
                       'reference': 'APT-123', 'occurred_at': datetime.now(UTC).isoformat(),
                       'test_mode': True}
            created = client.post(path + '/notifications', headers=headers, json=payload)
            assert created.status_code == 202, created.text
            assert client.post(path + '/notifications', headers=headers, json=payload).json()['id'] == created.json()['id']
            unlimited = client.put(path + '/route', headers=headers,
                                   json={'channel_id': channel['id'], 'daily_limit': None})
            assert unlimited.status_code == 200, unlimited.text
            assert client.get(path + '/route', headers=headers).json()['daily_limit'] is None
            for index in range(4):
                extra = client.post(path + '/notifications', headers=headers,
                                    json={**payload, 'request_key': f'booking-confirmed-extra-{index}'})
                assert extra.status_code == 202, extra.text
            saved = sql('SELECT recipient,body,kind,category FROM whatsapp_alert_deliveries WHERE id=%s',
                        (created.json()['id'],))[0]
            assert saved['recipient'] == recipient
            assert saved['kind'] == 'notification' and saved['category'] == 'booking.confirmed'
            assert ('Appointment confirmed' in saved['body']
                    and 'cannot make a payment' in saved['body'])
            assert client.delete(path + '/route', headers=headers).status_code == 204
        finally:
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))

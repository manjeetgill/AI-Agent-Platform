"""Recipient-controlled START must respect newer STOPs and webhook retries."""
import asyncio
import time
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform.whatsapp import ingest


def test_start_resumes_only_alert_recipient_and_never_queues_bot_reply(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        _, channel, doc = setup(client, key, monkeypatch)
        conversation = uuid4()
        recipient = '919876543210'
        sql('''INSERT INTO whatsapp_alert_routes(app_id,channel_id,enabled)
            VALUES(%s,%s,true)''', (key['app_id'], channel['id']))
        sql("""INSERT INTO whatsapp_conversations(id,channel_id,customer,mode,opted_out)
            VALUES(%s,%s,%s,'HUMAN',true)""", (conversation, channel['id'], recipient))

        def receive(body, mid, stamp):
            async def run():
                async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                    await ingest(pool, channel, {'entry': [{'changes': [{'value': {
                        'metadata': {'phone_number_id': channel['phone_id']},
                        'messages': [{'id': mid, 'from': recipient, 'type': 'text',
                                      'timestamp': str(stamp), 'text': {'body': body}}],
                    }}]}]})
            asyncio.run(run())

        def opted_out():
            return sql('SELECT opted_out FROM whatsapp_conversations WHERE id=%s', (conversation,))[0]['opted_out']

        try:
            stamp = int(time.time()) - 30
            mid = 'wamid.start.' + uuid4().hex
            receive(' START ', mid, stamp)
            assert not opted_out()
            assert sql('SELECT status FROM whatsapp_messages WHERE provider_id=%s', (mid,))[0]['status'] == 'CANCELLED'
            assert sql('SELECT mode FROM whatsapp_conversations WHERE id=%s', (conversation,))[0]['mode'] == 'HUMAN'
            # START opts back into the sender; it must not subscribe an app or send a message itself.
            assert not sql('SELECT 1 FROM whatsapp_alert_subscriptions WHERE app_id=%s', (key['app_id'],))
            receive('stop', 'wamid.stop.' + uuid4().hex, stamp + 10)
            assert opted_out()
            receive('START', mid, stamp)  # Retried old webhook.
            assert opted_out()
            receive('START', 'wamid.late.' + uuid4().hex, stamp + 5)  # Delayed older event.
            assert opted_out()
            receive('hi', 'wamid.hi.' + uuid4().hex, stamp + 15)
            assert opted_out()
            receive('START', 'wamid.new.' + uuid4().hex, stamp + 20)
            assert not opted_out()
            sql('UPDATE whatsapp_conversations SET opted_out=true WHERE id=%s', (conversation,))
            sql('UPDATE whatsapp_alert_routes SET enabled=false WHERE app_id=%s', (key['app_id'],))
            receive('START', 'wamid.disabled.' + uuid4().hex, stamp + 25)
            assert opted_out()
        finally:
            sql('DELETE FROM whatsapp_alert_routes WHERE app_id=%s', (key['app_id'],))
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))

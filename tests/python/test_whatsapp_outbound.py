"""Durable multi-recipient approval, isolation and uncertain-send tests."""
import asyncio
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from test_task_agents import database, environment, sql
from test_whatsapp import setup

from agent_platform import whatsapp_outbound as outbound
from agent_platform.whatsapp import ingest

TEMPLATE = {'id': '123456789', 'name': 'hello_world', 'language': 'en_US', 'text': 'Hello from the test business.'}


async def catalogue(config):
    return [TEMPLATE]


async def run_tick(sender):
    async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
        return await outbound.tick_outbound(pool, sender=sender, catalogue=catalogue)


def test_batch_approval_isolation_idempotency_and_delivery(monkeypatch):
    monkeypatch.setattr(outbound, 'templates', catalogue)
    with environment(monkeypatch) as (client, _, key, _):
        headers, channel, doc = setup(client, key, monkeypatch)
        path = '/v1/whatsapp/channels/'+channel['id']+'/outbound'
        rows = []
        calls = []

        async def sender(config, phone, recipient, template):
            calls.append(recipient)
            return 'wamid.'+recipient

        try:
            for recipient in ['919999000001', '919999000002']:
                body = {'recipient': recipient, 'template_id': TEMPLATE['id'], 'idempotency_key': uuid4().hex}
                r = client.post(path, headers=headers, json=body)
                assert r.status_code == 201, r.text
                rows.append(r.json())
                assert client.post(path, headers=headers, json=body).json()['id'] == rows[-1]['id']
                assert client.post(path, headers=headers, json={**body, 'recipient': '919999000003'}).status_code == 409
            assert asyncio.run(run_tick(sender)) is False
            approval = {'message_ids': [r['id'] for r in rows], 'confirm': True, 'recipient_consented': True}
            assert client.post(path+'/approve-batch', headers={**headers, 'X-External-User-ID': 'other'}, json=approval).status_code == 404
            assert client.post(path+'/approve-batch', headers=headers, json={**approval, 'recipient_consented': False}).status_code == 422
            assert client.post(path+'/approve-batch', headers=headers, json={**approval, 'message_ids': [rows[0]['id'], str(uuid4())]}).status_code == 404
            assert all(r['status'] == 'DRAFT' for r in client.get(path, headers=headers).json())
            assert client.post(path+'/approve-batch', headers=headers, json=approval).status_code == 200
            assert client.post(path+'/approve-batch', headers=headers, json=approval).status_code == 200
            assert asyncio.run(run_tick(sender))
            assert asyncio.run(run_tick(sender))
            assert not asyncio.run(run_tick(sender))
            assert calls == ['919999000001', '919999000002']
            assert all(r['status'] == 'ACCEPTED' for r in client.get(path, headers=headers).json())

            async def receipts():
                async with AsyncConnectionPool(database(), open=False, kwargs={'row_factory': dict_row}) as pool:
                    for status in ['read', 'sent']:
                        await ingest(pool, channel, {'entry': [{'changes': [{'value': {'metadata': {'phone_number_id': channel['phone_id']}, 'statuses': [{'id': 'wamid.919999000001', 'status': status}]}}]}]})
            asyncio.run(receipts())
            assert next(r for r in client.get(path, headers=headers).json() if r['recipient'] == '919999000001')['delivery'] == 'read'
        finally:
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))


def test_uncertain_send_and_optout_are_not_retried(monkeypatch):
    monkeypatch.setattr(outbound, 'templates', catalogue)
    with environment(monkeypatch) as (client, _, key, _):
        headers, channel, doc = setup(client, key, monkeypatch)
        path = '/v1/whatsapp/channels/'+channel['id']+'/outbound'
        calls = []
        async def sender(*args):
            calls.append(True)
            raise TimeoutError()
        try:
            row = client.post(path, headers=headers, json={'recipient': '919999000001', 'template_id': TEMPLATE['id'], 'idempotency_key': uuid4().hex}).json()
            endpoint = path+'/'+row['id']+'/approve'
            approval = {'confirm': True, 'recipient_consented': True}
            assert client.post(endpoint, headers=headers, json=approval).status_code == 200
            assert asyncio.run(run_tick(sender))
            assert client.get(path, headers=headers).json()[0]['status'] == 'UNKNOWN'
            client.post(endpoint, headers=headers, json=approval)
            assert not asyncio.run(run_tick(sender))
            # Emulate a crash rolling back the message transaction after committing its dispatch fence.
            sql("UPDATE whatsapp_outbound SET status='QUEUED' WHERE id=%s", (row['id'],))
            assert asyncio.run(run_tick(sender))
            assert len(calls) == 1
            sql("INSERT INTO whatsapp_conversations(id,channel_id,customer,opted_out) VALUES(%s,%s,'919999000002',true)", (uuid4(),channel['id']))
            row2 = client.post(path, headers=headers, json={'recipient': '919999000002', 'template_id': TEMPLATE['id'], 'idempotency_key': uuid4().hex}).json()
            client.post(path+'/'+row2['id']+'/approve', headers=headers, json=approval)
            assert asyncio.run(run_tick(sender))
            assert len(calls) == 1
            assert client.get(path, headers=headers).json()[0]['status'] == 'CANCELLED'
        finally:
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))


def test_session_notice_requires_real_open_chat_and_rechecks_before_send(monkeypatch):
    from test_whatsapp import inbound
    with environment(monkeypatch) as (client, _, key, _):
        headers, channel, doc = setup(client, key, monkeypatch)
        base='/v1/whatsapp/channels/'+channel['id']
        body={'recipient':'919999000001','text':'DEMO: Your appointment is delayed by 15 minutes.','idempotency_key':uuid4().hex}
        sent=[]
        async def sender(config, phone, recipient, template):
            sent.append(template)
            return 'wamid.session-test'
        try:
            assert client.post(base+'/session-outbound',headers=headers,json=body).status_code==409
            assert inbound(client,channel).status_code==200
            row=client.post(base+'/session-outbound',headers=headers,json=body)
            assert row.status_code==201,row.text
            draft=row.json()
            assert client.post(base+'/session-outbound',headers=headers,json=body).json()['id']==draft['id']
            assert client.post(base+'/session-outbound',headers=headers,json={**body,'text':'Different'}).status_code==409
            assert client.post(base+'/session-outbound',headers={**headers,'X-External-User-ID':'other'},json=body).status_code==404
            assert not asyncio.run(run_tick(sender))
            approve=base+'/outbound/'+draft['id']+'/approve'
            assert client.post(approve,headers=headers,json={'confirm':True,'recipient_consented':True}).status_code==200
            sql("UPDATE whatsapp_messages SET received_at=now()-interval '25 hours' WHERE conversation_id IN (SELECT id FROM whatsapp_conversations WHERE channel_id=%s)",(channel['id'],))
            assert asyncio.run(run_tick(sender))
            assert not sent
            assert client.get(base+'/outbound',headers=headers).json()[0]['status']=='FAILED'
            assert inbound(client,channel).status_code==200
            body['idempotency_key']=uuid4().hex
            draft=client.post(base+'/session-outbound',headers=headers,json=body).json()
            assert client.post(base+'/outbound/'+draft['id']+'/approve',headers=headers,json={'confirm':True,'recipient_consented':True}).status_code==200
            assert asyncio.run(run_tick(sender))
            assert sent[0]['kind']=='session'
            assert sent[0]['text']==body['text']
            assert not asyncio.run(run_tick(sender))
            for condition in ["mode='HUMAN'", "mode='AUTO',opted_out=true"]:
                sql('UPDATE whatsapp_conversations SET '+condition+' WHERE channel_id=%s',(channel['id'],))
                body['idempotency_key']=uuid4().hex
                assert client.post(base+'/session-outbound',headers=headers,json=body).status_code==409
        finally:
            sql('DELETE FROM whatsapp_channels WHERE id=%s',(channel['id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s',(doc['id'],))

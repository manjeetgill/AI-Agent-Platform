import json
from uuid import uuid4

import pytest
from test_task_agents import environment, sql
from test_whatsapp import clean, inbound, rows, setup, work

from agent_platform import sahayak_bridge


def test_clinic_worker_uses_bridge_and_preserves_delivery_fence(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET execution_mode='SAHAYAK' WHERE id=%s", (channel['id'],))
            calls = []
            async def clinic(c, event, customer, text, **kwargs):
                calls.append((str(event), customer, text))
                return {'replies': ['Appointment confirmed.'], 'handledBy': 'agent', 'redFlag': False}
            monkeypatch.setattr(sahayak_bridge, 'request', clinic)
            inbound(client, channel, text='YES', mid='bridge-confirm')
            inbound(client, channel, text='YES', mid='bridge-confirm')
            work()
            assert rows(channel)[0]['status'] == 'READY'
            sent = []
            async def send(*args):
                sent.append(args[-1])
                return uuid4().hex
            work(sender=send)
            work(sender=send)
            assert sent == ['Appointment confirmed.'] and len(calls) == 1
        finally:
            clean(key)


def test_missing_bridge_never_falls_back_to_rag(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET execution_mode='SAHAYAK' WHERE id=%s", (channel['id'],))
            monkeypatch.setenv('AI_SAHAYAK_BRIDGES', '{}')
            inbound(client, channel)
            work()
            assert rows(channel)[0]['status'] == 'UNKNOWN'
            assert not work()
        finally:
            clean(key)


def test_handoff_notice_sent_before_automatic_replies_stop(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            _, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET execution_mode='SAHAYAK' WHERE id=%s", (channel['id'],))
            async def clinic(*args, **kwargs):
                return {'replies': ['Reception will help you.'], 'handledBy': 'human', 'redFlag': False}
            monkeypatch.setattr(sahayak_bridge, 'request', clinic)
            inbound(client, channel, text='human')
            work()
            sent = []
            async def send(*args):
                sent.append(args[-1])
                return uuid4().hex
            work(sender=send)
            assert sent == ['Reception will help you.']
            assert sql('SELECT mode FROM whatsapp_conversations WHERE channel_id=%s', (channel['id'],))[0]['mode'] == 'HUMAN'
        finally:
            clean(key)


def test_activation_requires_reachable_clinic_bridge(monkeypatch):
    with environment(monkeypatch) as (client, _, key, _):
        try:
            headers, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET execution_mode='SAHAYAK',enabled=false WHERE id=%s", (channel['id'],))
            monkeypatch.setenv('AI_SAHAYAK_BRIDGES', '{}')
            response = client.post(f"/v1/whatsapp/channels/{channel['id']}/activation", headers=headers, json={'enabled': True})
            assert response.status_code == 409
            assert not sql('SELECT enabled FROM whatsapp_channels WHERE id=%s', (channel['id'],))[0]['enabled']
        finally:
            clean(key)


def test_binding_rejects_wrong_app_and_insecure_endpoint(monkeypatch):
    channel = {'id': uuid4(), 'app_id': uuid4()}
    cfg = {'app_id': str(uuid4()), 'clinic_id': str(uuid4()), 'url': 'https://clinic.example/bridge', 'secret': 'x'*32}
    monkeypatch.setenv('AI_SAHAYAK_BRIDGES', json.dumps({str(channel['id']): cfg}))
    with pytest.raises(ValueError):
        sahayak_bridge.binding(channel)
    cfg.update(app_id=str(channel['app_id']), url='http://clinic.example/bridge')
    monkeypatch.setenv('AI_SAHAYAK_BRIDGES', json.dumps({str(channel['id']): cfg}))
    with pytest.raises(ValueError):
        sahayak_bridge.binding(channel)


def test_clinic_binding_is_encrypted_idempotent_and_not_reassignable(monkeypatch):
    from test_meta_onboarding import settings
    settings(monkeypatch)
    monkeypatch.setenv('AI_SAHAYAK_ALLOWED_ORIGINS', '["https://clinic.example"]')
    with environment(monkeypatch) as (client, _, key, _):
        try:
            headers, channel, _ = setup(client, key, monkeypatch)
            sql("UPDATE whatsapp_channels SET enabled=false,execution_mode='SAHAYAK' WHERE id=%s", (channel['id'],))
            payload={'clinic_id':str(uuid4()),'url':'https://clinic.example/integrations/agent-platform/whatsapp','secret':'private-secret-'*4}
            route=f"/v1/whatsapp/channels/{channel['id']}/sahayak"
            assert client.put(route,headers=headers,json={**payload,'url':'https://other.example/integrations/agent-platform/whatsapp'}).status_code==422
            assert client.put(route,headers=headers,json=payload).json()=={'configured':True}
            assert client.put(route,headers=headers,json=payload).status_code==200
            stored=sql('SELECT config_cipher FROM sahayak_channel_bridges WHERE channel_id=%s',(channel['id'],))[0]
            assert payload['secret'] not in stored['config_cipher']
            assert client.put(route,headers=headers,json={**payload,'clinic_id':str(uuid4())}).status_code==409
            assert client.put(route,headers={**headers,'X-External-User-ID':'other'},json=payload).status_code==404
        finally:
            clean(key)

"""Customer preview uses the production knowledge boundary and never dispatches."""
from uuid import uuid4

from test_knowledge import Model
from test_task_agents import environment, sql
from test_whatsapp import setup

from agent_platform import knowledge


def test_preview_isolation_idempotency_and_inactive_edits(monkeypatch):
    model = Model()
    monkeypatch.setattr(knowledge, 'gateway', lambda: model)
    with environment(monkeypatch) as (client, _, key, _):
        headers, channel, doc = setup(client, key, monkeypatch)
        path = '/v1/whatsapp/channels/' + channel['id']
        body = {'question': 'What is the refund policy?', 'idempotency_key': uuid4().hex}
        try:
            wrong = {**headers, 'X-External-User-ID': 'different-customer'}
            assert client.post(path+'/preview', headers=wrong, json=body).status_code == 404
            changes = {'name': 'Customer support', 'documents': [doc['id']]}
            assert client.put(path, headers=headers, json=changes).status_code == 409
            assert client.post(path+'/activation', headers=headers, json={'enabled': False}).status_code == 200
            assert client.put(path, headers=headers, json={**changes, 'documents': [str(uuid4())]}).status_code == 422
            assert client.put(path, headers=wrong, json=changes).status_code == 404
            assert client.put(path, headers=headers, json=changes).status_code == 200
            result = client.post(path+'/preview', headers=headers, json=body)
            assert result.status_code == 200, result.text
            assert result.json()['sent_to_whatsapp'] is False
            assert not result.json()['handoff']
            assert result.json()['result']['citations'][0]['document_id'] == doc['id']
            assert client.post(path+'/preview', headers=headers, json=body).json() == result.json()
            assert model.calls == 1
            assert not sql('SELECT * FROM whatsapp_conversations WHERE channel_id=%s', (channel['id'],))
            # Unsharing the only allowed source makes the same cached preview unusable.
            sql("UPDATE knowledge_documents SET visibility='private' WHERE id=%s", (doc['id'],))
            assert client.post(path+'/preview', headers=headers, json=body).status_code == 409
            fresh = client.post(path+'/preview', headers=headers, json={**body, 'idempotency_key': uuid4().hex})
            assert fresh.status_code == 200 and fresh.json()['handoff']
            assert model.calls == 1
        finally:
            sql('DELETE FROM whatsapp_channels WHERE id=%s', (channel['id'],))
            sql('DELETE FROM knowledge_answers WHERE app_id=%s', (key['app_id'],))
            sql('DELETE FROM knowledge_documents WHERE id=%s', (doc['id'],))

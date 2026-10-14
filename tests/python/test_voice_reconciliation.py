import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_task_agents import environment, sql
from test_web_voice import Media, create

from agent_platform import web_voice
from agent_platform.voice_reconciliation import Charge, reconcile


def test_reviewed_invoice_atomic_idempotent_isolated_operator_only(monkeypatch):
    monkeypatch.setattr(web_voice, 'ready', lambda: True)
    monkeypatch.setattr(web_voice.calling_media, 'Session', Media)
    with environment(monkeypatch) as (client, headers, key, _):
        agent, _ = create(client, headers, key)
        response = client.post('/v1/voice/agents/'+agent['id']+'/sessions', headers=headers, json={
            'sdp':'v=0\r\n'+'fixture '*5,'request_key':uuid4().hex,'audio_consent':True})
        assert response.status_code == 201
        session = response.json()['id']
        ledger = uuid4()
        sql("""INSERT INTO voice_usage_ledger(id,session_id,app_id,stage,provider,model,unit,state,reserved_units,currency,rate,reserved_amount,rate_reference)
            VALUES(%s,%s,%s,'tts','fixture','synthetic','characters','UNKNOWN',10,'INR',1,10,'synthetic')""", (ledger,session,key['app_id']))
        line = Charge(ledger_id=ledger, app_id=key['app_id'], provider='fixture', model='synthetic', stage='tts', currency='INR', actual_amount=Decimal('4.2'), invoice_reference='synthetic-invoice', line_reference='1')
        async def run(lines, operator=True):
            async with await psycopg.AsyncConnection.connect(os.environ['AGENT_TEST_MIGRATION_URL' if operator else 'AGENT_TEST_DATABASE_URL'], row_factory=dict_row) as conn:
                return await reconcile(conn, lines)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            asyncio.run(run([line], False))
        assert asyncio.run(run([line]))['imported'] == 1
        assert asyncio.run(run([line]))['unchanged'] == 1
        with pytest.raises(ValueError, match='differs'):
            asyncio.run(run([line.model_copy(update={'actual_amount':Decimal(0)})]))
        with pytest.raises(ValueError, match='identity'):
            asyncio.run(run([line.model_copy(update={'app_id':uuid4()})]))
        usage = client.get('/v1/voice/usage',headers=headers).json()['accounting']['stages'][0]
        assert float(usage['held_amount']) == 0
        assert float(usage['reconciled_amount']) == 4.2
        assert float(usage['budget_amount']) == 4.2
        assert sql('SELECT state FROM voice_usage_ledger WHERE id=%s',(ledger,))[0]['state']=='UNKNOWN'
        with environment(monkeypatch) as (_, other, _, _):
            assert client.get('/v1/voice/usage',headers=other).json()['accounting']['stages']==[]
        # Failed batch must not partially reconcile the first new valid line.
        second=uuid4()
        sql("""INSERT INTO voice_usage_ledger SELECT %s,session_id,app_id,stage,provider,model,unit,state,reserved_units,units,currency,rate,reserved_amount,amount,rate_reference,provider_response_id,created_at,completed_at FROM voice_usage_ledger WHERE id=%s""",(second,ledger))
        fresh=line.model_copy(update={'ledger_id':second,'line_reference':'2'})
        with pytest.raises(ValueError):
            asyncio.run(run([fresh,line.model_copy(update={'model':'wrong'})]))
        assert not sql('SELECT * FROM voice_reconciliations WHERE ledger_id=%s',(second,))

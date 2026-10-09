from uuid import uuid4

from test_task_agents import environment, sql


def test_export_uses_recorded_usage_preserves_unknowns_and_owner_isolation(monkeypatch):
    monkeypatch.setenv('AI_AUTOMATIONS', '1')
    with environment(monkeypatch) as (client, headers, key, _):
        path='/v1/orchestration/usage/models'
        assert client.get(path,headers=headers).json()['rows']==[]
        assert client.get(path,headers={**headers,'X-External-User-ID':'other'}).status_code==200
        owners=sql('SELECT external_id,owner_id FROM platform_external_users WHERE app_id=%s',(key['app_id'],))
        tenant=sql('SELECT tenant_id FROM platform_apps WHERE id=%s',(key['app_id'],))[0]['tenant_id']
        try:
            for row in owners:
                for tokens,cost in ((20,'0.002'),(None,None)):
                    sql('''INSERT INTO platform_model_calls
                        (id,tenant_id,app_id,owner_id,operation,status,provider,model,total_tokens,latency_ms,estimated_cost)
                        VALUES(%s,%s,%s,%s,'fixture','COMPLETED','fixture','fixture-model',%s,10,%s)''',
                        (uuid4(),tenant,key['app_id'],row['owner_id'],tokens,cost))
            result=client.get(path,headers=headers).json()
            row=result['rows'][0]
            assert row['calls']==2 and row['reported_tokens']==20
            assert row['calls_with_unknown_usage']==1 and row['calls_with_unknown_cost']==1
            assert float(row['recorded_estimated_cost'])==0.002
            assert result['cost_currency'] is None and not result['truncated']
            assert client.get(path+'?days=367',headers=headers).status_code==422
        finally:
            sql('DELETE FROM platform_model_calls WHERE app_id=%s',(key['app_id'],))

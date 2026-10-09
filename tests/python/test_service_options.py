from test_task_agents import environment, sql

from agent_platform.service_options import Choice


def grant(key):
    sql("UPDATE platform_service_keys SET scopes=scopes||ARRAY['services:read','services:write'] WHERE id=%s",(key['key_id'],))


def payload(route='platform', provider='smtp', revision=0):
    return {'capability':'email','route':route,'provider':provider,'expected_revision':revision}


def test_scoped_choices_are_setup_only_isolated_and_versioned(monkeypatch):
    with environment(monkeypatch) as (a,ha,ka,_), environment(monkeypatch) as (b,hb,kb,_):
        path='/v1/integrations/service-options'
        assert a.get(path,headers=ha).status_code==403
        grant(ka);grant(kb)
        result=a.get(path,headers=ha).json()
        assert result['account_owner']=='platform' and not result['customer_provider_account_required']
        assert all(s['choice'] is None and s['price'] is None for s in result['services'])
        saved=a.post(path,headers=ha,json=payload())
        assert saved.status_code==200,saved.text
        assert saved.json()=={'saved':True,'revision':1,'status':'SETUP_REQUESTED','activated':False}
        assert a.post(path,headers=ha,json=payload()).json()==saved.json()
        assert all(s['choice'] is None for s in b.get(path,headers=hb).json()['services'])
        assert a.post(path,headers=ha,json=payload('provider','resend',0)).status_code==409
        assert a.post(path,headers=ha,json=payload('provider','resend',1)).json()['revision']==2
        assert a.post(path,headers=ha,json=payload('platform','sarvam',2)).status_code==422
        assert a.post(path,headers=ha,json={**payload(),'app_id':kb['app_id']}).status_code==422
        rows=sql('SELECT * FROM platform_service_choice_events WHERE app_id=%s ORDER BY revision',(ka['app_id'],))
        assert len(rows)==2 and [r['provider'] for r in rows]==['smtp','resend']
        assert not sql('SELECT * FROM notification_email_jobs WHERE app_id=%s',(ka['app_id'],))
        sql("UPDATE platform_service_keys SET scopes=ARRAY['services:read'] WHERE id=%s",(ka['key_id'],))
        assert a.post(path,headers=ha,json=payload()).status_code==403


def test_operator_choices_do_not_expand_app_permissions(tmp_path,monkeypatch):
    from test_local_monitor import INTENT, configure
    with environment(monkeypatch) as (client,headers,key,_):
        monkeypatch.setenv('AI_LOCAL_ONBOARDING','1')
        configure(tmp_path,monkeypatch,[('fixture',key)])
        path='/monitor/setup/fixture/services'
        assert client.get(path,headers=INTENT).status_code==200
        assert client.post(path,headers=INTENT,json=payload()).status_code==200
        assert client.post(path,json=payload()).status_code==403
        assert client.post(path,headers={**INTENT,'Origin':'https://foreign.example'},json=payload()).status_code==403
        assert client.get('/v1/integrations/service-options',headers=headers).status_code==403


def test_public_option_matrix_rejects_mismatched_services():
    import pytest
    for capability,route,provider in [('speech','platform','sarvam'),('whatsapp','provider','meta'),('email','platform','brevo')]:
        with pytest.raises(ValueError):
            Choice(capability=capability,route=route,provider=provider)

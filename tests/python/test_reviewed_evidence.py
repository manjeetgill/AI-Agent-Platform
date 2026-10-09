import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from agent_platform.connector_health import CheckInput, ProbeResult, evaluate
from agent_platform.reviewed_evidence import ReviewedEvidence, evidence_path, read_evidence


def save(root, app, owner, observation):
    raw=json.dumps({'schema_version':1,'acquisition':'operator_reviewed_browser',
        'app_id':str(app),'owner_id':str(owner),'observation':observation}).encode()
    digest=hashlib.sha256(raw).hexdigest()
    path=evidence_path(root,app,owner,digest)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(raw)
    return digest,path


def observation():
    return {'source_url':'https://www.jiomart.com/product/example-123',
        'observation_id':'browser-fixture','observed_at':datetime.now(UTC).isoformat(),
        'price':62.0,'currency':'INR','title':'Fixture butter','variant':'100 g'}


def test_digest_and_owner_isolation(tmp_path):
    app,owner=uuid4(),uuid4()
    digest,path=save(tmp_path,app,owner,observation())
    assert read_evidence(tmp_path,app,owner,digest).price==62
    with pytest.raises(OSError):read_evidence(tmp_path,uuid4(),owner,digest)
    with pytest.raises(OSError):read_evidence(tmp_path,app,uuid4(),digest)
    path.write_text(path.read_text().replace('62.0','1.0'))
    with pytest.raises(ValueError):read_evidence(tmp_path,app,owner,digest)
    with pytest.raises(ValueError):ReviewedEvidence(None,None).prepare({'evidence_id':'../../secret'})


def test_manual_location_conflicts_and_staleness_are_not_usable():
    obs={**observation(),'location_required':True,'quality_issues':['variant_conflict'],
         'observed_at':(datetime.now(UTC)-timedelta(hours=2)).isoformat()}
    probe=ProbeResult(status='WORKING',reason_code='operator_reviewed_browser_observation',
                      observations=[obs],verification_mode='stored_observation')
    rows,usable=evaluate(probe,CheckInput(tool='evidence.reviewed',version='1',require_price=True))
    assert not usable
    assert set(rows[0]['issues'])=={'variant_conflict','location_unconfirmed','stale'}
    assert rows[0]['unknown_fees']


def test_worker_reads_reviewed_evidence_without_contacting_source(monkeypatch,tmp_path):
    from test_connector_checks import drive, result, setup
    from test_task_agents import sql
    monkeypatch.setenv('AI_REVIEWED_EVIDENCE_ROOT',str(tmp_path.resolve()))
    with setup(monkeypatch) as (client,headers,key,registry,_):
        registry.adapters['evidence.reviewed']=ReviewedEvidence(registry.adapters['runtime.connector_check'].pool,registry)
        sql('INSERT INTO platform_tool_grants(app_id,tool) VALUES(%s,%s)',(key['app_id'],'evidence.reviewed'))
        assert client.get('/v1/connectors',headers=headers).status_code==200
        owner=sql('SELECT owner_id FROM platform_external_users WHERE app_id=%s',(key['app_id'],))[0]['owner_id']
        digest,_=save(tmp_path.resolve(),key['app_id'],owner,observation())
        a=client.post('/v1/task-agents',headers=headers,json={'idempotency_key':uuid4().hex,
            'blueprint':{'name':'Review browser evidence','description':'Read operator observation',
                'steps':[{'tool':'evidence.reviewed','version':'1','arguments':{'evidence_id':digest}}]}})
        assert a.status_code==201,a.text
        t=client.post('/v1/task-agents/'+a.json()['id']+'/runs',headers=headers,json={'idempotency_key':uuid4().hex})
        assert t.status_code==202,t.text
        drive(registry)
        report=result(client,headers,t.json()['id'])
        assert report['verification_mode']=='stored_observation'
        assert report['usable_evidence'] and report['evidence'][0]['price']==62

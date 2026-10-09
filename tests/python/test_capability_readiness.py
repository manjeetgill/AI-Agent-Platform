import asyncio

import httpx
from test_task_agents import environment

from agent_platform.capability_readiness import SOURCES, checklist, collect


def test_passive_checks_use_fixed_gets_and_do_not_request_ungranted_sources():
    calls = []
    def response(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.host == 'internal'
        assert request.url.path in {path for _, path in SOURCES.values()}
        return httpx.Response(503, json={'detail': 'secret upstream response'})
    async def run():
        async with httpx.AsyncClient(base_url='http://internal', transport=httpx.MockTransport(response)) as c:
            return await collect(c, {'notifications:read'})
    report = checklist(asyncio.run(run()))
    assert len(calls) == 1 and calls[0].url.path.endswith('/email/configuration')
    assert len(report) == 11
    assert 'secret upstream response' not in str(report)
    assert all(not item['live_verified'] for item in report)
    assert {r['id']: r['status'] for r in report}['planning'] == 'ACCESS_REQUIRED'


def test_mock_and_configured_never_claim_live_success_or_return_secret_fields():
    sources = {name: {'status':200, 'data':{}} for name in SOURCES}
    sources['email']['data'] = {'configured':True,'mock':True,'provider':'mock','webhook_configured':False,
                                'access_token':'never-return-this'}
    sources['wa_setup']['data'] = {'public_url':'https://example.com','worker_online':True}
    sources['wa_channels']['data'] = [{'enabled':True,'webhook_verified_at':'2026-01-01','phone_id':'private-number'}]
    sources['voice']['data'] = {'configured':True,'speech_provider':'sarvam'}
    sources['voice_agents']['data'] = {'agents':[{'enabled':True,'name':'private-business-name'}]}
    report = {c['id']:c for c in checklist(sources)}
    assert report['email']['status'] == report['email_tracking']['status'] == 'MOCK_ONLY'
    assert report['whatsapp']['status'] == report['internet_voice']['status'] == 'VERIFICATION_REQUIRED'
    assert report['internet_voice']['evidence']['speech_provider'] == 'sarvam'
    for private in ('never-return-this','private-number','private-business-name'):
        assert private not in str(report)


def test_http_contract_is_scoped_and_does_not_invoke_planner(monkeypatch):
    with environment(monkeypatch) as (client, headers, key, planner):
        path='/v1/integrations/capability-readiness'
        assert client.get(path).status_code == 401
        assert client.get(path,headers={**headers,'X-External-User-ID':''}).status_code == 422
        response=client.get(path,headers=headers)
        assert response.status_code == 200, response.text
        report=response.json()
        assert report['app_id'] == key['app_id']
        assert len(report['capabilities']) == 11 and not report['provider_checks_performed']
        assert planner.calls == 0
        assert {c['id']:c['status'] for c in report['capabilities']}['email'] == 'ACCESS_REQUIRED'
        assert key['credential'] not in response.text


def test_console_knowledge_routes_support_search_and_owner_checked_delete():
    from agent_platform.console_app import PATH
    assert PATH.fullmatch('knowledge/search')
    assert PATH.fullmatch('knowledge/documents/00000000-0000-0000-0000-000000000000')
    assert not PATH.fullmatch('knowledge/documents/../../secrets')
    assert PATH.fullmatch('integrations/capability-readiness')


def test_model_diagnostics_preserve_privacy_and_identify_quota():
    from agent_platform.planning_errors import explanation
    from agent_platform.runtime_errors import RunFailure
    for status, phrase in [(401,'private API key'),(403,'private API key'),(404,'model ID'),(429,'quota')]:
        error=RunFailure('MODEL_UNAVAILABLE')
        request=httpx.Request('POST','https://provider.example',headers={'Authorization':'private-key'})
        error.__cause__=httpx.HTTPStatusError('secret raw error',request=request,
            response=httpx.Response(status,text='secret raw body',request=request))
        message=explanation(error)
        assert phrase in message and 'secret raw' not in message and 'private-key' not in message
    assert 'timed out' in explanation(TimeoutError())
    assert 'invalid plan' in explanation(RunFailure('INVALID_MODEL_OUTPUT'))


def test_whatsapp_token_failure_is_actionable_and_does_not_expose_provider_body(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from agent_platform import whatsapp_outbound
    client_type=httpx.AsyncClient
    def response(request):
        assert request.method=='GET' and request.url.host=='graph.facebook.com'
        assert len(request.url.params['appsecret_proof'])==64
        assert 'app-secret-value' not in str(request.url)
        return httpx.Response(400,json={'error':{'code':190,'message':'private token value'}})
    monkeypatch.setattr(whatsapp_outbound.httpx,'AsyncClient',
        lambda **kwargs:client_type(**kwargs,transport=httpx.MockTransport(response)))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(whatsapp_outbound.graph({'graph_version':'v26.0','access_token':'fixture',
                                           'app_secret':'app-secret-value'},'123456'))
    assert 'invalid or expired' in failure.value.detail
    assert 'private token value' not in failure.value.detail

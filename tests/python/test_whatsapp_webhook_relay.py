from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from agent_platform.whatsapp_webhook_relay import create_relay


def test_relay_exposes_only_webhooks_and_preserves_signed_bytes():
    calls = []
    def upstream(request):
        calls.append(request)
        assert request.url.host == '127.0.0.1'
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        assert 'x-console-intent' not in request.headers
        if request.method == 'GET':
            assert request.url.params['hub.verify_token'] == 'test-verification'
            return httpx.Response(200, text='test-challenge')
        assert request.content == b'{ "signed": true }'
        assert request.headers['x-hub-signature-256'] == 'sha256=test'
        return httpx.Response(403, json={'detail':'Invalid webhook signature'})
    client = TestClient(create_relay(httpx.MockTransport(upstream)))
    for path in ('/', '/whatsapp', '/console/api/whatsapp/setup', '/console/whatsapp/connection', '/docs', '/openapi.json'):
        assert client.get(path).status_code == 404
    hook = '/webhooks/whatsapp/' + str(uuid4())
    assert client.get(hook, params={'hub.mode':'subscribe','hub.verify_token':'test-verification','hub.challenge':'test-challenge'}).text == 'test-challenge'
    result = client.post(hook, content=b'{ "signed": true }', headers={
        'X-Hub-Signature-256':'sha256=test','Authorization':'must-not-forward','Cookie':'must-not-forward',
        'X-Console-Intent':'user-action'})
    assert result.status_code == 403
    assert client.post(hook, content=b'x'*256001).status_code == 413
    assert len(calls) == 2


def test_hosted_relay_is_scoped_and_keeps_secure_headers():
    calls=[]
    def upstream(request):
        calls.append(request)
        assert request.headers['cookie']=='meta_signup=test-ticket'
        assert 'authorization' not in request.headers
        assert 'x-console-intent' not in request.headers
        assert request.headers['origin']=='https://signup.example.test'
        return httpx.Response(200,json={'ready':True},headers={'Set-Cookie':'meta_signup=test-ticket; Secure; HttpOnly; SameSite=Strict','Content-Security-Policy':"default-src 'none'"})
    client=TestClient(create_relay(httpx.MockTransport(upstream)))
    root='/connect/whatsapp/'+str(uuid4())
    r=client.post(root+'/bootstrap',headers={'Origin':'https://signup.example.test','Cookie':'meta_signup=test-ticket; operator=private','Authorization':'private','X-Console-Intent':'user-action'},json={})
    assert r.status_code==200 and 'HttpOnly' in r.headers['set-cookie']
    assert r.headers['content-security-policy']=="default-src 'none'"
    for path in (root+'/admin','/connect/whatsapp-assets/other.js','/v1/whatsapp/connections','/console/api/whatsapp/onboarding'):
        assert client.get(path).status_code==404
    assert len(calls)==1

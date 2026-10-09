"""Unified workspace proxy must preserve origin checks and upstream app scopes."""
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.console_app import local
from agent_platform.workspace_shell import ASSETS, LINKS, install_bridge, shell


def test_shared_navigation_preserves_existing_controls():
    for name,path in [('index.html','/workspace'),('dashboard.html','/'),('whatsapp.html','/whatsapp'),
                      ('calling.html','/calling'),('voice.html','/voice'),('telephony.html','/telephony'),
                      ('memory.html','/memory'),('feeds.html','/connectors'),('email.html','/monitor/email')]:
        source=shell((ASSETS/name).read_text(),path)
        assert source.count('aria-current="page"')>=1
        assert '<nav class="workspace-nav" aria-label="Platform capabilities">' in source
        assert '/console/workspace.css' in source
        for url,_ in LINKS:
            assert 'href="'+url+'"' in source
        if name=='index.html':
            assert 'id="pair-browser"' in source and 'id="agents"' in source


def test_bridge_is_fixed_origin_and_preserves_upstream_permissions():
    calls=[]
    def upstream(request):
        calls.append(request)
        assert str(request.url).startswith('http://127.0.0.1:4060/monitor')
        assert 'authorization' not in request.headers and 'cookie' not in request.headers and 'origin' not in request.headers
        if request.url.path=='/monitor/email':
            return httpx.Response(200,text='<html><head></head><body><main>Email</main></body></html>',headers={'content-type':'text/html'})
        return httpx.Response(403,json={'detail':'Missing application scope'})
    app=FastAPI();install_bridge(app,local,transport=httpx.MockTransport(upstream))
    with TestClient(app) as c:
        r=c.get('/monitor/email',headers={'Authorization':'Bearer browser-secret','Cookie':'secret=private'})
        assert r.status_code==200 and 'Platform capabilities' in r.text
        r=c.get('/monitor/api/example/notifications/email',headers={'X-Monitor-Intent':'user-action'})
        assert r.status_code==403 and r.json()['detail']=='Missing application scope'
        assert c.post('/monitor/setup/apps',json={}).status_code==403
        assert c.post('/monitor/setup/apps',headers={'X-Monitor-Intent':'user-action','Origin':'https://evil.example'},json={}).status_code==403
        assert c.get('/monitor',headers={'Host':'public.example'}).status_code==403
        assert c.get('/monitor',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
        assert c.post('/monitor/setup/apps',headers={'X-Monitor-Intent':'user-action'},content=b'x'*200001).status_code==413
        assert len(calls)==2


def test_upstream_outage_keeps_workspace_navigation():
    def offline(request):
        raise httpx.ConnectError('private infrastructure detail',request=request)
    app=FastAPI();install_bridge(app,local,transport=httpx.MockTransport(offline))
    with TestClient(app) as c:
        r=c.get('/monitor');assert r.status_code==503
        assert 'Back to overview' in r.text and 'href="/whatsapp"' in r.text
        assert 'private infrastructure' not in r.text
        r=c.get('/monitor/connections',headers={'X-Monitor-Intent':'user-action'})
        assert r.status_code==503 and 'private infrastructure' not in r.text

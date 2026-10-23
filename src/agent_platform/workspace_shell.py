"""Shared local workspace navigation and fixed-target operations bridge."""
import html
import os
import re
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

ASSETS = Path(__file__).with_name('console')
LINKS = [('/', 'Overview'),('/setup','Setup checklist'),('/workspace','Build agents'),('/whatsapp','WhatsApp'),
         ('/calling','WhatsApp calling'),('/voice','Internet voice'),('/telephony','Telephone'),
         ('/memory','Memory & knowledge'),('/connectors','API & feeds'),
         ('/monitor','Tasks & readiness'),('/monitor/email','Email'),('/monitor/sms','SMS'),('/monitor/onboard','Connected apps'),
         ('/self-hosting','Self-hosting')]
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'"


def shell(source, path):
    matches=[url for url,_ in LINKS if path==url or url!='/' and path.startswith(url+'/')]
    selected=max(matches,key=len) if matches else '/'
    links=''.join('<a href="'+url+'"'+(' aria-current="page"' if url==selected else '')+'>'+html.escape(label)+'</a>' for url,label in LINKS)
    header='<div class="workspace-bar"><a class="workspace-brand" href="/">◈ Agent Workspace</a><span>AI Agent Platform · Local operator</span></div>'
    nav='<a class="workspace-skip" href="#workspace-content">Skip navigation</a><header class="workspace-header">'+header+'<nav class="workspace-nav" aria-label="Platform capabilities">'+links+'</nav></header>'
    source=source.replace('</head>','<link rel="stylesheet" href="/console/workspace.css"></head>',1)
    source=re.sub(r'(<body\b[^>]*>)',lambda m:m[0]+nav+'<div id="workspace-content" class="workspace-content">',source,count=1)
    return source.replace('</body>','</div></body>',1)


def page(name, path):
    return HTMLResponse(shell((ASSETS/name).read_text(),path),headers={'Cache-Control':'no-store','Content-Security-Policy':CSP})


def install_bridge(app, local, *, transport=None):
    # create_app installs same-process monitor routes. This console has a separate
    # pilot database; use the real operations service instead of misrouting app keys.
    app.router.routes[:]=[r for r in app.router.routes if not getattr(r,'path','').startswith('/monitor')]
    # The console forwards to the independently configured operations service.
    # Its own database's monitor flag must not intercept these proxy requests.
    app.state.workspace_monitor_bridge = True

    @app.api_route('/monitor',methods=['GET'],include_in_schema=False)
    @app.api_route('/monitor/{path:path}',methods=['GET','POST'],include_in_schema=False)
    async def bridge(request:Request,path:str=''):
        local(request)
        if request.headers.get('sec-fetch-site')=='cross-site':
            raise HTTPException(403,'Cross-origin access denied')
        if request.method=='POST' and request.headers.get('x-monitor-intent')!='user-action':
            raise HTTPException(403,'Use the workspace for this action')
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>200000:
                raise HTTPException(413,'Request too large')
        # Rebuild from a fixed trusted base. Never forward cookies, authorization,
        # Origin or a user-selected URL; upstream scopes remain authoritative.
        target='/monitor'+('/'+path if path else '')
        if any(part in {'.','..'} for part in path.split('/')) or '\\' in path:
            raise HTTPException(404)
        headers={'X-Monitor-Intent':request.headers.get('x-monitor-intent',''),
                 'Content-Type':'application/json'}
        base = os.environ.get('AI_PLATFORM_URL', 'http://127.0.0.1:4060')
        try:
            async with httpx.AsyncClient(base_url=base,timeout=15,
                                         follow_redirects=False,trust_env=False,transport=transport) as client:
                response=await client.request(request.method,target,params=request.query_params,headers=headers,content=bytes(raw))
        except httpx.HTTPError:
            if request.method=='GET' and path in {'','email','sms','onboard'}:
                source='<!doctype html><html><head><title>Service unavailable · Agent Workspace</title></head><body><main><h1>Application service unavailable</h1><p>Start the local platform API and worker, then refresh. Your other workspace screens remain available.</p><a href="/">Back to overview</a></main></body></html>'
                return HTMLResponse(shell(source,request.url.path),status_code=503,headers={'Cache-Control':'no-store','Content-Security-Policy':CSP})
            raise HTTPException(503,'Application service unavailable; other workspace capabilities remain available.') from None
        content_type=response.headers.get('content-type','application/json')
        if 'text/html' in content_type:
            return HTMLResponse(shell(response.text,request.url.path),status_code=response.status_code,
                                headers={'Cache-Control':'no-store','Content-Security-Policy':CSP})
        return Response(response.content,status_code=response.status_code,headers={'Content-Type':content_type,'Cache-Control':'no-store'})

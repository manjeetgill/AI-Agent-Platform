"""Read-only setup checklist composed from fixed, permission-checked local APIs.

No provider probes, test messages, model calls or activation occur on a GET.
Configured state is never reported as proof of successful delivery/live audio.
"""
import asyncio
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .integration_catalog import catalogue
from .service_identity import authenticate
from .task_api import credential_contract

# Fixed paths only. In particular, calling/agents/{id}/readiness performs a
# provider probe and must never be invoked by this passive inventory.
SOURCES = {
    'planning': ('tasks:read', '/v1/integrations/task-readiness'),
    'wa_setup': ('channels:read', '/v1/whatsapp/setup'),
    'wa_channels': ('channels:read', '/v1/whatsapp/channels'),
    'calling': ('calling:read', '/v1/calling/agents'),
    'voice': ('agents:read', '/v1/voice/capabilities'),
    'voice_agents': ('agents:read', '/v1/voice/agents'),
    'telephone': ('telephony:read', '/v1/telephony/connections'),
    'sms': ('notifications:read', '/v1/notifications/sms/configuration'),
    'email': ('notifications:read', '/v1/notifications/email/configuration'),
    'knowledge': ('knowledge:read', '/v1/knowledge/documents'),
    'feeds': ('tasks:read', '/v1/connectors/feeds'),
    'schedules': ('tasks:read', '/v1/orchestration/automations'),
}


async def collect(client, scopes):
    semaphore = asyncio.Semaphore(4)

    async def read(name, permission, path):
        if permission not in scopes:
            return name, {'status': 403}
        try:
            async with semaphore, asyncio.timeout(3):
                response = await client.get(path)
                payload = response.json() if response.status_code == 200 else None
                if response.status_code == 200:
                    expected = dict if name in {'planning', 'wa_setup', 'voice', 'sms', 'email'} else (dict, list)
                    if not isinstance(payload, expected):
                        return name, {'status': 503}
                return name, {'status': response.status_code,
                              'data': payload}
        except Exception:  # noqa: BLE001 -- isolate errors without returning private upstream data
            return name, {'status': 503}

    return dict(await asyncio.gather(*(read(k, *v) for k, v in SOURCES.items())))


def checklist(sources):
    result = []

    def data(name):
        return sources.get(name, {}).get('data') or {}

    def rows(name, key):
        value = data(name)
        items = value if isinstance(value, list) else value.get(key, [])
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def add(code, title, dependencies, configured, detail, steps, link, *, mock=False, evidence=None):
        statuses = [sources.get(d, {}).get('status', 503) for d in dependencies]
        state = ('ACCESS_REQUIRED' if 403 in statuses else 'UNAVAILABLE' if any(s != 200 for s in statuses)
                 else 'MOCK_ONLY' if mock else 'VERIFICATION_REQUIRED' if configured else 'CONFIGURATION_REQUIRED')
        if state == 'ACCESS_REQUIRED':
            detail = 'This application needs the appropriate permission or tool grant to inspect this capability.'
        elif state == 'UNAVAILABLE':
            detail = 'Setup state could not be read. Check service configuration and refresh; other capabilities remain available.'
        result.append({'id': code, 'title': title, 'status': state, 'summary': detail,
                       'next_steps': steps, 'workspace_url': link, 'evidence': evidence or {},
                       'live_verified': False})

    p = data('planning')
    add('planning', 'Natural-language planning', ['planning'], p.get('ready', False),
        'Review scopes, tool grants, remaining quota and worker health before requesting a plan.',
        ['Resolve blocked task-readiness checks.', 'Draft a synthetic request, review its plan and run it through the worker.'],
        '/workspace', evidence={'checks': p.get('checks', []), 'last_planning_call': p.get('model', {}).get('last_planning_call')})
    w = data('wa_setup'); channels = rows('wa_channels', 'channels')
    add('whatsapp', 'WhatsApp messaging', ['wa_setup', 'wa_channels'],
        bool(w.get('worker_online') and w.get('public_url') and any(c.get('enabled') and c.get('webhook_verified_at') for c in channels)),
        f'{len(channels)} channels in this page; {sum(bool(c.get("enabled")) for c in channels)} enabled.',
        ['Save authorised sender credentials privately and check the connection.',
         'Configure public HTTPS and verify the provider webhook.',
         'Activate the channel, then send a test from an authorised recipient and confirm its reply.'], '/whatsapp',
        evidence={'worker_online': w.get('worker_online', False), 'public_https_configured': bool(w.get('public_url')),
                  'verified_channels_in_page': sum(bool(c.get('webhook_verified_at')) for c in channels)})
    calls = rows('calling', 'agents')
    add('whatsapp_calling', 'WhatsApp calling', ['calling'], any(a.get('enabled') for a in calls),
        f'{len(calls)} calling agents in this page. Number eligibility requires a provider check.',
        ['Connect an eligible business number and check its calling readiness.',
         'Configure the audio worker and app handler; complete an approved real call test.'], '/calling')
    voice = data('voice'); agents = rows('voice_agents', 'agents')
    add('internet_voice', 'Internet voice', ['voice', 'voice_agents'],
        bool(voice.get('configured') and any(a.get('enabled') for a in agents)),
        f'{len(agents)} voice agents in this page. Saved settings do not verify the microphone or audio output.',
        ['Publish FAQs or connect the app business-action handler.',
         'Run a text rehearsal, then use Talk to AI and verify both directions of audio.'], '/voice',
        evidence={'runtime_configured': voice.get('configured', False), 'speech_provider': voice.get('speech_provider')})
    tel = rows('telephone', 'connections')
    add('telephone', 'Telephone calling', ['telephone'], any(c.get('enabled') and c.get('carrier_verified_at') for c in tel),
        f'{len(tel)} telephone connections in this page.',
        ['Business supplies an authorised carrier connection and number.',
         'Configure Asterisk routing and gateway, verify readiness, then test an inbound call.'], '/telephony')
    email = data('email')
    add('email', 'Real email sending', ['email'], email.get('configured', False),
        'Mock delivery sends no real email.' if email.get('mock') else 'Provider configuration needs a verified sender and a receipt test.',
        ['Configure Resend or Brevo, a verified sender, private key and usage limits.',
         'Submit a test through the queue and confirm receipt.'], '/monitor/email', mock=email.get('mock', False),
        evidence={'provider': email.get('provider'), 'limits': email.get('limits')})
    add('email_tracking', 'Email delivery tracking', ['email'], email.get('webhook_configured', False),
        'A saved webhook secret does not prove the provider callback is registered or reachable.',
        ['Register the public HTTPS email callback and matching authentication at the provider.',
         'Verify a signed delivery event; API acceptance alone is not delivery.'], '/monitor/email', mock=email.get('mock', False))
    if sources.get('email', {}).get('status') == 200 and email.get('provider') == 'smtp':
        result[-1].update(status='NOT_SUPPORTED', summary='SMTP relay acceptance does not verify recipient delivery.',
                          next_steps=['Use provider delivery callbacks through Resend/Brevo when delivery tracking is required.'])
    sms = data('sms')
    add('sms', 'SMS notifications', ['sms'], sms.get('configured', False),
        'Mock mode sends no SMS. Provider acceptance does not verify handset delivery.',
        ['Configure an authorised SMS provider or a private Android SIM pilot with allowed test recipients.',
         'Set segment limits, then verify one approved test on the recipient phone.'], '/monitor/sms',
        mock=sms.get('mock', False), evidence={'provider':sms.get('provider'), 'limits':sms.get('limits')})
    docs = rows('knowledge', 'documents')
    add('knowledge', 'Knowledge / RAG', ['knowledge'], bool(docs),
        f'{len(docs)} accessible documents in this page. Lexical retrieval works without an embedding subscription.',
        ['Add app documents with the correct private/shared visibility.',
         'Test retrieval, then ask the model and verify the returned citations.'], '/memory')
    feeds = rows('feeds', 'connections')
    add('feeds', 'API / feed connectors', ['feeds'], any(f.get('state') == 'READY' and not f.get('disabled') for f in feeds),
        f'{len(feeds)} connections in this page. Source access and stale evidence must be checked independently.',
        ['Configure an approved source profile and private credentials if required.',
         'Run the bounded connection test; inspect evidence and preserve source backoff.'], '/connectors')
    schedules = rows('schedules', 'automations')
    add('schedules', 'Scheduled source checks', ['schedules'], any(s.get('status') == 'ACTIVE' for s in schedules),
        f'{len(schedules)} automations in this page. These may include tasks other than source checks.',
        ['Test the source adapter first.', 'Prepare and approve a bounded source-check schedule; inspect its first worker result.'], '/connectors')
    # Pairing is deliberately optional, and no secrets/device identifiers are returned.
    tools = p.get('tools', [])
    paired_tools = [t for t in tools if t.get('execution_location') == 'paired_browser' or t.get('tool', '').startswith('browser.')]
    add('browser', 'Chrome browser runner (optional)', ['planning'], bool(paired_tools),
        'A browser tool grant does not prove that an extension is paired or online.',
        ['Pair the extension and approve the required sites only for supported browser tasks.',
         'Run a read-only browser task. Mobile apps should use server tools and approved feeds.'], '/workspace')
    return result


def install(app):
    router = APIRouter(prefix='/v1/integrations', dependencies=[Depends(credential_contract)])

    async def identity(request):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer apk_'):
            raise HTTPException(401, 'Application service credential required')
        return await authenticate(request.app.state.repository.pool, authorization,
                                  request.headers.get('X-External-User-ID'), None)

    @router.get('/capabilities')
    async def capabilities(request: Request):
        claims = await identity(request)
        return {'app_id': claims['app_id'], 'contract_version': 1,
                'capabilities': catalogue(claims['scopes']),
                'readiness_url': '/v1/integrations/capability-readiness',
                'note': 'Permissions are not activation. Tools, provider authorization, limits and live tests are required separately.'}

    @router.get('/capability-readiness')
    async def readiness(request: Request):
        claims = await identity(request)
        authorization = request.headers.get('authorization', '')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://internal',
                                    headers={'Authorization': authorization,
                                             'X-External-User-ID': request.headers['X-External-User-ID']}) as client:
            sources = await collect(client, set(claims['scopes']))
        return {'app_id': claims['app_id'], 'checked_at': datetime.now(UTC),
                'capabilities': checklist(sources), 'integration_contracts': catalogue(claims['scopes']),
                'provider_checks_performed': False,
                'note': 'Saved state for this application and user only. Counts cover the first page; configuration is not proof of a successful live test.'}

    app.include_router(router)

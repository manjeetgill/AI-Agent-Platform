"""Read-only operator view of self-managed transports; no credentials are returned."""
import html
import json
import os

from fastapi import Request
from fastapi.responses import HTMLResponse

from .email_config import Config, api_key
from .workspace_shell import CSP, shell


def status():
    from .local_speech import assets

    try:
        assets()
        speech_installed = True
    except (ValueError, OSError):
        speech_installed = False
    email = {'mock': 0, 'smtp': 0, 'resend': 0, 'brevo': 0, 'configuration_required': 0}
    try:
        for value in json.loads(os.environ.get('AI_EMAIL_APPS', '{}')).values():
            try:
                configured = Config.model_validate(value)
                api_key(configured)
                email[configured.provider] += 1
            except (ValueError, TypeError, AttributeError):
                email['configuration_required'] += 1
    except (ValueError, TypeError, AttributeError):
        email['configuration_required'] += 1
    return {'speech': {'selected': os.environ.get('AI_CALLING_SPEECH_PROVIDER', 'gemini'),
                       'local_assets_present': speech_installed, 'live_audio_verified': False},
            'email_app_routes': email,
            'whatsapp': {'direct_meta_supported': True, 'meta_authorization_required': True},
            'scope': 'This local process configuration only; provider accounts and live delivery are not checked.'}


def install(app, local):
    @app.get('/console/self-hosting')
    async def configuration(request: Request):
        local(request)
        return status()

    @app.get('/self-hosting')
    async def home(request: Request):
        local(request)
        state = status()
        selected = html.escape(state['speech']['selected'])
        assets = 'Installed; an audio test is still required' if state['speech']['local_assets_present'] else 'Setup required: whisper-cli, multilingual model and espeak-ng'
        email = state['email_app_routes']
        source = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Self-hosting · Agent Workspace</title><link rel="stylesheet" href="/console/style.css"></head><body><main class="workspace-overview">
<h1>Run services yourself</h1><p class="intro">Your apps keep the same platform APIs. Choose who operates the speech and email services behind them.</p>
<section class="workspace-note">{html.escape(state['scope'])} This page does not activate services or send messages. <a href="/monitor/onboard#service-options">Choose our service or a provider-backed option for an app →</a></section>
<div class="capability-grid"><section class="capability-card"><h2>Offline speech</h2><p>Selected speech transport: <strong>{selected}</strong></p><p>{assets}</p><p>whisper.cpp recognises speech; eSpeak NG speaks the reply. No Sarvam speech account is needed. Voice quality and Hindi/English accuracy need testing.</p><p>The answer model is configured separately and may still use an external API.</p><a href="/voice">Open voice agents →</a></section>
<section class="capability-card"><h2>Your email server</h2><p>SMTP routes: <strong>{email['smtp']}</strong>. Mock routes: {email['mock']}. Resend: {email['resend']}. Brevo: {email['brevo']}. Incomplete: {email['configuration_required']}.</p><p>Use the existing notification API with your own SMTP relay. The queue, encrypted payloads, limits and audit records stay in the platform.</p><p>SMTP acceptance is not confirmed inbox delivery. Configure domain authentication and monitor your mail server for bounces.</p><a href="/monitor/email">Open email activity →</a></section>
<section class="capability-card"><h2>WhatsApp directly with Meta</h2><p>Direct Meta connector: <strong>Available</strong></p><p>Connect an authorized business number in WhatsApp setup, verify the HTTPS webhook, and test replies before activation. Chakra is optional.</p><p>Meta still operates WhatsApp. Its authorization, eligibility requirements and applicable charges remain.</p><a href="/whatsapp">Open Meta connection setup →</a></section></div>
<h2>Operator setup</h2><p>Follow <code>docs/SELF_HOSTED_SERVICES.md</code> in the platform repository for the speech and per-app SMTP configuration. Credentials stay on the server. Existing routes stay active until you explicitly change them.</p>
<p>Running these services yourself uses your own compute, storage and network. Telephone calls still require an authorized carrier connection.</p></main></body></html>'''
        return HTMLResponse(shell(source, '/self-hosting'), headers={'Cache-Control': 'no-store', 'Content-Security-Policy': CSP})

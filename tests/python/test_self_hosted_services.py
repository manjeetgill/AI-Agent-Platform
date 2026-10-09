import asyncio
import hashlib
import json
import smtplib
import textwrap
from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform import calling_audio, email_smtp, local_speech
from agent_platform.console_app import local
from agent_platform.email_config import Config
from agent_platform.self_hosting import install


def settings(**smtp):
    return Config(provider='smtp', from_email='sender@example.com',
                  smtp={'host': '127.0.0.1', 'port': 2525, 'security': 'loopback', **smtp})


EMAIL = {'from_name': 'App', 'from_email': 'sender@example.com', 'to': 'recipient@example.com',
         'subject': 'Test', 'text': 'Private OTP 912341', 'html': '<p>Private OTP 912341</p>'}


class Relay:
    failure = None
    raw = None
    closed = False
    calls: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        if self.failure == 'connect':
            raise OSError('private relay error')

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, sender):
        self.calls.append('mail')
        return (550 if self.failure == 'sender' else 250), b'private server text'

    def rcpt(self, recipient):
        self.calls.append('rcpt')
        return 250, b'ok'

    def data(self, raw):
        self.calls.append('data')
        type(self).raw = raw
        if self.failure == 'disconnect':
            raise smtplib.SMTPServerDisconnected('private recipient OTP')
        if self.failure == 'reject':
            raise smtplib.SMTPDataError(451, b'private retry text')
        return 250, b'accepted'

    def close(self):
        type(self).closed = True


@pytest.mark.parametrize('failure,status,uncertain', [(None,'ACCEPTED',False), ('connect','RETRY',False),
    ('sender','FAILED',False), ('disconnect','OUTCOME_UNKNOWN',True), ('reject','RETRY',False)])
def test_smtp_definitive_and_uncertain_outcomes(monkeypatch, failure, status, uncertain):
    monkeypatch.setattr(Relay, 'failure', failure)
    monkeypatch.setattr(Relay, 'calls', [])
    monkeypatch.setattr(email_smtp.smtplib, 'SMTP', Relay)
    result = asyncio.run(email_smtp.SMTPAdapter().send(settings(), EMAIL, str(uuid4())))
    assert result.status == status and result.uncertain is uncertain
    assert 'private' not in repr(result).lower() and EMAIL['to'] not in repr(result)
    if failure == 'sender':
        assert Relay.calls == ['mail']
    if status == 'ACCEPTED':
        assert result.provider_id.startswith('<') and Relay.closed
        assert b'Message-ID:' in Relay.raw and b'multipart/alternative' in Relay.raw


def test_smtp_security_and_existing_fingerprints():
    for params in [{'host':'mail.example.com'}, {'username':'user'}, {'host':'localhost'}]:
        with pytest.raises(ValueError):
            settings(**params)
    with pytest.raises(ValueError):
        Config(provider='smtp', from_email='x@example.com', smtp={'host':'mail.example.com','username':'user'})
    previous = {'provider':'mock','from_email':'x@example.com','from_name':'Notifications',
                'key_env':None,'allowed_link_hosts':[]}
    assert Config(**previous).fingerprint == hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()


def test_tls_is_required_before_auth_or_payload(monkeypatch):
    class NoTLS(Relay):
        def starttls(self, **kw):
            raise smtplib.SMTPNotSupportedError('no TLS')
    monkeypatch.setattr(Relay, 'calls', [])
    monkeypatch.setattr(email_smtp.smtplib, 'SMTP', NoTLS)
    outcome = email_smtp.submit(settings(host='mail.example.com',security='starttls'), EMAIL, str(uuid4()))
    assert outcome.status == 'FAILED' and outcome.code == 'smtp_security_configuration'
    assert Relay.calls == []


def local_fixture(monkeypatch, tmp_path, body):
    model = tmp_path/'model.bin';model.write_bytes(b'fixture-only')
    program = tmp_path/'whisper-cli'
    program.write_text('#!/usr/bin/env python3\n'+textwrap.dedent(body));program.chmod(0o700)
    monkeypatch.setenv('AI_LOCAL_STT_MODEL',str(model))
    monkeypatch.setenv('AI_LOCAL_STT_BINARY',str(program))
    monkeypatch.setenv('AI_LOCAL_TTS_BINARY',str(program))
    monkeypatch.setenv('AI_LOCAL_SPEECH_TMPDIR',str(tmp_path))
    monkeypatch.setenv('AI_CALLING_SPEECH_PROVIDER','local')
    return model, program


def test_offline_speech_routes_without_cloud_and_cleans_audio(monkeypatch,tmp_path):
    local_fixture(monkeypatch,tmp_path,'''
        import json, pathlib, sys, wave
        assert '-l' in sys.argv and sys.argv[sys.argv.index('-l')+1] == 'auto'
        with wave.open(sys.argv[sys.argv.index('-f')+1]) as w:
            assert w.getframerate()==16000 and w.getnchannels()==1
        target=sys.argv[sys.argv.index('-of')+1]+'.json'
        pathlib.Path(target).write_text(json.dumps({'transcription':[{'text':'appointment कल चाहिए'}]}))
    ''')
    assert calling_audio.speech_key() is None
    assert asyncio.run(calling_audio.transcribe(b'\0\0'*1600)) == 'appointment कल चाहिए'
    assert not list(tmp_path.glob('agent-speech-*'))
    async def synthesis(text):
        return b'\0\0',16000
    monkeypatch.setattr(calling_audio,'local_speak',synthesis)
    assert asyncio.run(calling_audio.speak('hello')) == (b'\0\0',16000)


def test_offline_failure_and_cancellation_cleanup(monkeypatch,tmp_path):
    local_fixture(monkeypatch,tmp_path,'import time; time.sleep(60)')
    async def cancel():
        task=asyncio.create_task(local_speech.transcribe(b'\0\0'*1600))
        await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert not list(tmp_path.glob('agent-speech-*'))
    monkeypatch.setenv('AI_LOCAL_STT_MODEL',str(tmp_path/'missing'))
    with pytest.raises(ValueError):
        asyncio.run(calling_audio.transcribe(b'\0\0'))


def test_self_hosting_is_local_only_and_redacted(monkeypatch):
    monkeypatch.setenv('AI_EMAIL_APPS',json.dumps({'private-app':settings().model_dump()}))
    monkeypatch.setenv('AI_CALLING_SPEECH_PROVIDER','<script>')
    app=FastAPI();install(app,local)
    with TestClient(app) as client:
        response=client.get('/self-hosting')
        assert response.status_code==200 and '&lt;script&gt;' in response.text
        assert '<script>' not in response.text and 'private-app' not in response.text
        status=client.get('/console/self-hosting').json()
        assert status['email_app_routes']['smtp']==1
        assert 'sender@example.com' not in json.dumps(status)
        assert client.get('/console/self-hosting',headers={'Host':'foreign.example'}).status_code==403
        assert client.get('/self-hosting',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403


def test_smtp_worker_queue_and_ambiguous_replay_guard(monkeypatch):
    from test_email_notifications import body, drive, get, post, setup, sql
    monkeypatch.setattr(email_smtp.smtplib, 'SMTP', Relay)
    monkeypatch.setattr(Relay, 'failure', None)
    monkeypatch.setattr(Relay, 'calls', [])
    with setup(monkeypatch) as (client, headers, key, _):
        monkeypatch.setenv('AI_EMAIL_APPS',json.dumps({key['app_id']:settings().model_dump()}))
        first=post(client,headers)
        assert drive()
        sent=get(client,headers,first['id'])
        assert sent['status']=='ACCEPTED' and sent['delivery_status']=='UNCONFIRMED'
        conf=client.get('/v1/notifications/email/configuration',headers=headers).json()
        assert not conf['delivery_confirmation_supported'] and not conf['webhook_configured']
        monkeypatch.setattr(Relay,'failure','disconnect')
        data=body();second=post(client,headers,data)
        assert drive()
        result=get(client,headers,second['id'])
        assert result['status']=='OUTCOME_UNKNOWN'
        assert post(client,headers,data)['id']==second['id']
        calls=len(Relay.calls)
        assert not drive() and len(Relay.calls)==calls
        assert sql('SELECT payload_cipher FROM notification_email_jobs WHERE id=%s',(second['id'],))[0]['payload_cipher'] is None


def test_smtp_real_loopback_protocol():
    # A local sink acknowledges SMTP but never relays externally. Exercise MIME/wire handling.
    async def run():
        captured=[]
        async def relay(reader,writer):
            writer.write(b'220 fixture ESMTP\r\n');await writer.drain()
            try:
                while line:=await reader.readline():
                    if line.upper().startswith(b'DATA'):
                        writer.write(b'354 Continue\r\n');await writer.drain()
                        captured.append(await reader.readuntil(b'\r\n.\r\n'))
                        writer.write(b'250 queued\r\n')
                    else:
                        writer.write(b'250 OK\r\n')
                    await writer.drain()
            finally:
                writer.close();await writer.wait_closed()
        server=await asyncio.start_server(relay,'127.0.0.1',0)
        async with server:
            port=server.sockets[0].getsockname()[1]
            result=await email_smtp.SMTPAdapter().send(settings(port=port),EMAIL,str(uuid4()))
            assert result.status=='ACCEPTED'
            assert len(captured)==1 and b'Message-ID:' in captured[0]
    asyncio.run(run())

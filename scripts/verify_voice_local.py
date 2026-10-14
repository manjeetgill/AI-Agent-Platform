"""Real local WebRTC/ASR/model/TTS pilot using synthetic speech, never a microphone or phone.

Requires isolated workspace at 4161 configured for local speech and a local model.
Creates a labelled synthetic FAQ agent. No cloud-provider fallback is permitted.
"""
import argparse
import asyncio
import fractions
import json
import time
from pathlib import Path
from uuid import uuid4

import av
import httpx
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parents[1]


def resample(raw, rate, target):
    frame = av.AudioFrame(format='s16', layout='mono', samples=len(raw)//2)
    frame.planes[0].update(raw)
    frame.sample_rate = rate
    converter = av.AudioResampler(format='s16', layout='mono', rate=target)
    return b''.join(bytes(f.planes[0])[:f.samples*2] for f in converter.resample(frame)+converter.resample(None))


async def main(barge_in=False, *, streaming=False, api_url=None, headers=None, save_report=True, after_connected=None, existing_agent_id=None, question='What are the clinic opening hours?'):
    import os

    from agent_platform import calling_audio, voice_config

    config = json.loads((ROOT/'.runtime/voice/config.json').read_text())
    os.environ.update(config)
    assert voice_config.defaults().asr == voice_config.defaults().tts == voice_config.defaults().reasoning == 'local'
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    received = bytearray()
    capture_tasks = []
    sent = asyncio.Event()
    heard = asyncio.Event()
    audio_stats = {'frames': 0, 'last_loud': 0.0, 'input_done': None, 'first_reply': None}
    started = time.monotonic()
    pcm, rate = await calling_audio.local_speak(question)
    outgoing = bytearray(resample(pcm, rate, 48000))

    class Speech(MediaStreamTrack):
        kind = 'audio'

        def __init__(self):
            super().__init__()
            self.pts = 0
            self.started = None

        async def recv(self):
            if self.started is None:
                self.started = time.monotonic()
            await asyncio.sleep(max(0, self.started+self.pts/48000-time.monotonic()))
            data = bytes(outgoing[:1920]) if sent.is_set() else b''
            if sent.is_set():
                del outgoing[:len(data)]
                if not outgoing and audio_stats['input_done'] is None:
                    audio_stats['input_done'] = time.monotonic()
            frame = av.AudioFrame(format='s16', layout='mono', samples=960)
            frame.planes[0].update(data.ljust(1920, b'\0'))
            frame.sample_rate, frame.pts, frame.time_base = 48000, self.pts, fractions.Fraction(1,48000)
            self.pts += 960
            return frame

    async def listen(track):
        from array import array

        converter = av.AudioResampler(format='s16', layout='mono', rate=16000)
        while True:
            frame = await track.recv()
            for f in converter.resample(frame):
                raw = bytes(f.planes[0])[:f.samples*2]
                audio_stats['frames'] += 1
                samples = array('h', raw)
                if samples and max(abs(x) for x in samples)>500:
                    heard.set()
                    audio_stats['last_loud'] = time.monotonic()
                    if audio_stats['input_done'] is not None and audio_stats['first_reply'] is None:
                        audio_stats['first_reply'] = time.monotonic()
                if sent.is_set() and len(received) < 16000*2*60:
                    received.extend(raw)

    @pc.on('track')
    def track(incoming):
        capture_tasks.append(asyncio.create_task(listen(incoming)))

    pc.addTrack(Speech())
    session = None
    async with httpx.AsyncClient(base_url=api_url or 'http://127.0.0.1:4161/console/api/', timeout=60,
            headers=headers or {'X-Console-Intent': 'user-action'}) as client:
        async def request(path, payload=None):
            response = await (client.get(path) if payload is None else client.post(path, json=payload))
            if response.status_code >= 400:
                raise RuntimeError(f'Local verification API failed: {response.status_code} at {path}')
            return response.json()

        try:
            capability = await request('voice/capabilities')
            assert capability['configured'] and capability['reasoning_provider']=='local'
            unique = uuid4().hex
            if existing_agent_id:
                agent = next(a for a in (await request('voice/agents'))['agents'] if a['id']==existing_agent_id)
                if not agent['name'].startswith('Local voice demo'):
                    raise ValueError('Existing-agent verification is limited to the synthetic local demo')
            else:
                doc = await request('knowledge/documents', {'external_id':'voice-pilot-'+unique,
                    'title':'Synthetic voice test clinic', 'text':'The test clinic opening hours are nine in the morning to five in the evening, Monday to Friday.',
                    'source_label':'Synthetic test fixture, not a real clinic', 'visibility':'app'})
                agent = await request('voice/agents', {'external_id':'voice-pilot-'+unique,
                    'name':'Local voice verification', 'greeting':'Welcome to the synthetic test clinic.',
                    'documents':[doc['id']], 'config':{**voice_config.defaults().model_dump(), 'stream_grounded_quotes':streaming}, 'language':'en'})
            await pc.setLocalDescription(await pc.createOffer())
            result = await request('voice/agents/'+agent['id']+'/sessions', {
                'sdp':pc.localDescription.sdp, 'request_key':unique, 'audio_consent':True, 'transcript_consent':True})
            session = result['id']
            await pc.setRemoteDescription(RTCSessionDescription(sdp=result['sdp'], type='answer'))
            await asyncio.wait_for(heard.wait(), timeout=25)
            if after_connected:
                await after_connected(session)
            if not barge_in:
                while time.monotonic()-audio_stats['last_loud']<1:
                    await asyncio.sleep(.2)
            received.clear()
            sent.set()
            deadline = time.monotonic()+65
            timeline = []
            while time.monotonic()<deadline:
                timeline = (await request('voice/sessions/'+session+'/events'))['events']
                if any(e['kind']=='turn_failed' for e in timeline):
                    raise RuntimeError('Local audio turn failed')
                answers = [e for e in timeline if e['kind']=='assistant']
                if answers and (any(e['kind']=='tts' and e['sequence']>answers[-1]['sequence'] for e in timeline) or (streaming and any(e['kind']=='streamed_claim' for e in timeline))):
                    await asyncio.sleep(2)
                    while time.monotonic()-audio_stats['last_loud']<1:
                        await asyncio.sleep(.2)
                    break
                await asyncio.sleep(.5)
            else:
                raise TimeoutError('No synthesized answer before deadline')
            # Verify all returned speech; taking only the last 12 seconds can
            # cut off the opening hour in longer answers and mislabel good audio.
            from array import array
            raw_reply=bytes(received)
            voiced=[offset for offset in range(0,len(raw_reply),640)
                    if any(abs(x)>500 for x in array('h',raw_reply[offset:offset+640]))]
            if not voiced:raise AssertionError('Returned audio contained no audible speech')
            raw_reply=raw_reply[max(0,voiced[0]-3200):voiced[-1]+3840]
            transcripts=[]
            for offset in range(0,len(raw_reply),320000):
                part=raw_reply[max(0,offset-16000):offset+352000]
                if part:transcripts.append(await calling_audio.transcribe(part))
            transcript=' '.join(transcripts)
            answer = next(e['content'] for e in timeline if e['kind']=='assistant')
            if save_report:
                (ROOT/'.runtime/voice/audio-attempt.json').write_text(json.dumps({'answer':answer,'transcription':transcript,'bytes':len(received),'events':timeline},default=str))
            else:
                # Labelled synthetic fixtures only; preserve failed-load diagnostics privately.
                (ROOT/f'.runtime/voice/load-{session}.json').write_text(json.dumps({'answer':answer,'transcription':transcript,'bytes':len(received),'events':timeline},default=str))
            assert ('nine' in answer.lower() or '9' in answer) and ('five' in answer.lower() or '5' in answer), 'Model did not ground the requested hours'
            assert ('nine' in transcript.lower() or '9' in transcript) and ('five' in transcript.lower() or '5' in transcript), 'Returned WebRTC audio did not contain the answer'
            assert audio_stats['first_reply'] is not None and audio_stats['input_done'] is not None
            await request('voice/sessions/'+session+'/end', {})
            final = await request('voice/sessions/'+session)
            assert final['state'] == 'ENDED', 'Completed audio session was not ended'
            for _ in range(20):
                timeline = (await request('voice/sessions/'+session+'/events'))['events']
                if any(e['kind']=='ended' for e in timeline):
                    break
                await asyncio.sleep(.1)
            assert any(e['kind']=='ended' for e in timeline)
            if barge_in:
                assert any(e['kind']=='interrupted' for e in timeline), 'Speaking over the greeting did not interrupt playback'
            if streaming:
                assert any(e['kind']=='streamed_claim' for e in timeline), 'No streamed source quotation'
                assert next(e['sequence'] for e in timeline if e['kind']=='streamed_claim') < next(e['sequence'] for e in timeline if e['kind']=='reasoning')
            report = {'status':'PASS', 'streaming_tested':streaming, 'synthetic_audio':True, 'human_microphone_tested':False,
                'barge_in_tested':barge_in,
                'telephone_tested':False, 'provider_fees':0, 'model':voice_config.defaults().model,
                'asr_model':Path(config['AI_LOCAL_STT_MODEL']).name, 'language':config.get('AI_LOCAL_STT_LANGUAGE', 'auto'),
                'session_id':session, 'agent_id':agent['id'], 'duration_seconds':round(time.monotonic()-started,2),
                'last_input_frame_to_first_received_audio_ms':round((audio_stats['first_reply']-audio_stats['input_done'])*1000),
                'final_state':final['state'],
                'received_frames':audio_stats['frames'], 'spoken_reply_transcription':transcript,
                'events':timeline}
            if save_report:
                output = ROOT/'docs/verification/voice-local-e2e.json'
                output.write_text(json.dumps(report, indent=2, default=str)+'\n')
                runs = output.with_name('voice-local-e2e-runs.json')
                previous = json.loads(runs.read_text()) if runs.exists() else []
                runs.write_text(json.dumps((previous+[report])[-10:], indent=2, default=str)+'\n')
                print(json.dumps({k:v for k,v in report.items() if k!='events'}))
            return report
        finally:
            if session:
                await request('voice/sessions/'+session+'/end', {})
            for task in capture_tasks:
                task.cancel()
            await asyncio.gather(*capture_tasks, return_exceptions=True)
            await pc.close()


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--barge-in', action='store_true', help='Speak over the greeting and require an interruption event')
    parser.add_argument('--streaming', action='store_true')
    parser.add_argument('--existing-agent', help='ID of the labelled synthetic local demo only')
    parser.add_argument('--question', default='What are the clinic opening hours?', help='Synthetic opening-hours question variant; the same factual answer is still required')
    args=parser.parse_args()
    asyncio.run(main(args.barge_in, streaming=args.streaming, existing_agent_id=args.existing_agent, question=args.question))

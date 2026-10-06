"""Optional Sarvam speech transport for the platform's existing bounded voice turns.

No phone provisioning, auto recharge, provider retries, or raw audio persistence.
"""
import base64
import io
import json
import os
import re
import wave

import httpx


class SarvamSpeechError(ValueError):
    pass


def key():
    value = os.environ.get('AI_SARVAM_API_KEY', '').strip()
    if not value:
        raise SarvamSpeechError('Configure private Sarvam API access')
    return value


async def request(path, **kwargs):
    try:
        async with (
            httpx.AsyncClient(timeout=httpx.Timeout(20, connect=3), follow_redirects=False) as client,
            client.stream('POST', 'https://api.sarvam.ai/'+path,
                          headers={'api-subscription-key': key()}, **kwargs) as response,
        ):
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 4_000_000:
                    raise ValueError('Response too large')
            return json.loads(raw)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise SarvamSpeechError('Sarvam speech unavailable; no automatic retry') from None


async def transcribe(pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > 384000:
        raise SarvamSpeechError('Audio must be at most 12 seconds of mono 16kHz PCM')
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    result = await request('speech-to-text', files={'file': ('turn.wav', out.getvalue(), 'audio/wav')},
                           data={'model': 'saaras:v3', 'mode': 'codemix', 'language_code': 'unknown'})
    transcript = result.get('transcript') if isinstance(result, dict) else None
    if not isinstance(transcript, str) or len(transcript) > 1500:
        raise SarvamSpeechError('Invalid Sarvam transcript')
    return transcript


async def speak(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 1200:
        raise SarvamSpeechError('Speech text must contain 1..1200 characters')
    result = await request('text-to-speech', json={
        'text': text, 'language_code': 'hi-IN' if re.search(r'[\u0900-\u097f]', text) else 'en-IN',
        'model': 'bulbul:v3', 'speaker': 'shubh', 'speech_sample_rate': 16000,
        'output_audio_codec': 'wav',
    })
    try:
        audios = result['audios']
        if not isinstance(audios, list) or len(audios) != 1:
            raise ValueError('Invalid audio count')
        raw = base64.b64decode(audios[0], validate=True)
        with wave.open(io.BytesIO(raw), 'rb') as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
                raise ValueError('Invalid audio format')
            if not 0 < wav.getnframes() <= 16000 * 45:
                raise ValueError('Invalid audio duration')
            pcm = wav.readframes(wav.getnframes())
            if len(pcm) != wav.getnframes() * 2:
                raise ValueError('Truncated audio')
            return pcm, 16000
    except (ValueError, KeyError, TypeError, wave.Error, EOFError):
        raise SarvamSpeechError('Invalid Sarvam speech audio') from None

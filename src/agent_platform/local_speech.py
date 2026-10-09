"""Offline whisper.cpp recognition; binaries/models are installed by the operator.

No API credentials, runtime downloads or cloud fallback. Short-lived audio files
are private and removed after each turn (use a tmpfs directory when required).
"""
import asyncio
import json
import os
import shutil
import tempfile
import wave
from pathlib import Path


def assets():
    binary = shutil.which(os.environ.get('AI_LOCAL_STT_BINARY', 'whisper-cli'))
    tts = shutil.which(os.environ.get('AI_LOCAL_TTS_BINARY', 'espeak-ng'))
    model = Path(os.environ.get('AI_LOCAL_STT_MODEL', ''))
    if not binary or not tts or not model.is_absolute() or not model.is_file():
        raise ValueError('Install whisper-cli and espeak-ng and set AI_LOCAL_STT_MODEL to a local multilingual model')
    language = os.environ.get('AI_LOCAL_STT_LANGUAGE', 'auto')
    if language not in {'auto', 'en', 'hi'}:
        raise ValueError('Local speech language must be auto, en or hi')
    return binary, model, language


async def transcribe(pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > 384000:
        raise ValueError('Audio must be at most 12 seconds of mono 16kHz PCM')
    binary, model, language = assets()
    # Bound CPU work per process. The existing voice runtime bounds concurrent calls.
    with tempfile.TemporaryDirectory(prefix='agent-speech-', dir=os.environ.get('AI_LOCAL_SPEECH_TMPDIR')) as folder:
        source, output = Path(folder)/'input.wav', Path(folder)/'result'
        with wave.open(str(source), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        process = await asyncio.create_subprocess_exec(
            binary, '-m', str(model), '-f', str(source), '-l', language,
            '-t', '2', '-nt', '-oj', '-of', str(output),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        result = output.with_suffix('.json')
        if process.returncode or not result.is_file() or result.stat().st_size > 65536:
            raise ValueError('Local transcription failed')
        try:
            segments = json.loads(result.read_text())['transcription']
            if not isinstance(segments, list) or any(not isinstance(s.get('text'), str) for s in segments):
                raise ValueError()
            return ''.join(s['text'] for s in segments).strip()[:1500]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError('Invalid local transcription result') from None

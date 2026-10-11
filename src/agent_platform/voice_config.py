"""Versionable voice configuration. Secrets/endpoints belong to the operator, never callers."""
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_serializer, model_validator

from .gateway import FallbackProvider, Gateway, NVIDIAProvider
from .tasks import Strict


class Specialist(Strict):
    name: str = Field(min_length=1, max_length=40, pattern=r'^[a-z][a-z0-9 ]*$')
    agent_id: UUID

    @field_serializer('agent_id')
    def serialize_agent(self, value: UUID) -> str:
        return str(value)


class VoiceConfig(Strict):
    specialists: list[Specialist] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def distinct_specialists(self) -> "VoiceConfig":
        names=[s.name for s in self.specialists]
        if len(set(names)) != len(names) or 'main assistant' in names:
            raise ValueError('Use unique specialist names; main assistant is reserved')
        return self

    asr: Literal['gemini', 'sarvam', 'local'] = 'gemini'
    tts: Literal['gemini', 'sarvam', 'local'] = 'gemini'
    reasoning: Literal['gemini', 'nvidia', 'openrouter', 'together', 'local'] = 'gemini'
    model: str = Field(default='gemini-3.1-flash-lite', min_length=1, max_length=200, pattern=r'^[a-zA-Z0-9_./:-]+$')
    asr_model: str = Field(default='gemini-3.1-flash-lite', min_length=1, max_length=120, pattern=r'^[a-zA-Z0-9_.-]+$')
    tts_model: str = Field(default='gemini-2.5-flash-preview-tts', min_length=1, max_length=120, pattern=r'^[a-zA-Z0-9_.-]+$')
    voice: Literal['Kore', 'Puck', 'Charon', 'Fenrir', 'Aoede'] = 'Kore'
    barge_in: bool = True
    partial_transcripts: bool = False
    stream_grounded_quotes: bool = False
    silence_ms: int = Field(default=800, ge=300, le=2000)
    speech_threshold: int = Field(default=300, ge=100, le=3000)
    turn_timeout_seconds: int = Field(default=45, ge=5, le=60)


active: ContextVar[VoiceConfig | None] = ContextVar('voice_configuration', default=None)


def defaults() -> VoiceConfig:
    speech = os.environ.get('AI_CALLING_SPEECH_PROVIDER', 'gemini')
    return VoiceConfig.model_validate({'asr': speech, 'tts': speech,
        'reasoning': os.environ.get('AI_WEB_VOICE_PROVIDER', 'gemini'),
        'model': os.environ.get('AI_WEB_VOICE_MODEL', 'gemini-3.1-flash-lite'),
        'asr_model': os.environ.get('AI_CALLING_STT_MODEL', 'gemini-3.1-flash-lite'),
        'tts_model': os.environ.get('AI_CALLING_TTS_MODEL', 'gemini-2.5-flash-preview-tts')})


def current() -> VoiceConfig:
    return active.get() or defaults()


@contextmanager
def using(value: Mapping[str, Any] | VoiceConfig | None) -> Iterator[VoiceConfig]:
    config = VoiceConfig.model_validate(value) if value else defaults()
    token = active.set(config)
    try:
        yield config
    finally:
        active.reset(token)


def gateway() -> Gateway:
    config = current()
    provider: NVIDIAProvider
    if config.reasoning == 'gemini':
        from .calling_audio import key
        provider = FallbackProvider('gemini', key())
    elif config.reasoning == 'nvidia':
        provider = NVIDIAProvider(os.environ.get('NVIDIA_API_KEY', ''))
    elif config.reasoning == 'local':
        if config.model.endswith(':cloud') or config.model.endswith('-cloud'):
            raise ValueError('Cloud model cannot be billed as local processing')
        # Fixed operator configuration, never an endpoint supplied by an API caller.
        from urllib.parse import urlsplit
        url = os.environ.get('AI_VOICE_LOCAL_MODEL_URL', '')
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError('Configure a loopback local model endpoint')
        provider = NVIDIAProvider(os.environ.get('AI_VOICE_LOCAL_MODEL_KEY', '') or 'local-no-auth', url)
        provider.name = 'local'
    else:
        provider = FallbackProvider(config.reasoning, os.environ.get('AI_VOICE_' + config.reasoning.upper() + '_API_KEY', ''))
    if not provider.key:
        raise ValueError('Selected voice model provider is not configured')
    # A pinned voice model must not be changed by general task-routing preferences.
    return Gateway(provider, max_attempts=1, allow_fallback=False, allow_chain=False, model_override=config.model)

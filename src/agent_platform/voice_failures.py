"""Public-safe failure codes. Never expose upstream exception text."""
import httpx

CODES = frozenset({'TIMEOUT', 'MODEL_TIMEOUT', 'MODEL_UNAVAILABLE', 'INVALID_MODEL_OUTPUT',
    'PROVIDER_BACKPRESSURE', 'MODEL_NOT_CONFIGURED', 'CONTEXT_BUDGET_EXCEEDED',
    'PROVIDER_AUTH', 'PROVIDER_QUOTA', 'PROVIDER_ERROR', 'SPENDING_LIMIT',
    'RATE_CONFIGURATION_REQUIRED', 'FAILED'})


class VoiceUsageError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def failure_code(error):
    code = getattr(error, 'code', None)
    if isinstance(code, str) and code in CODES:
        return code
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return 'TIMEOUT'
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return 'PROVIDER_AUTH' if status in {401, 403} else 'PROVIDER_QUOTA' if status == 429 else 'PROVIDER_ERROR'
    return 'FAILED'


def public_failure(details):
    stages = {'asr', 'tts', 'reasoning', 'business_action', 'audio_turn', 'stream_completion', 'greeting'}
    return {'outcome': details.get('outcome') if details.get('outcome') in CODES else 'FAILED',
            'stage': details.get('stage') if details.get('stage') in stages else 'audio_turn'}

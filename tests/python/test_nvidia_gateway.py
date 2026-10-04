import asyncio

import httpx
import pytest

from agent_platform.gateway import Gateway, NVIDIAProvider
from agent_platform.runtime_errors import RunFailure

MODEL = 'nvidia/nemotron-3-ultra-550b-a55b'


def test_ultra_request_uses_plain_json_without_reasoning_budget():
    provider = NVIDIAProvider('test-key')
    _, _, payload = provider.request(MODEL, 'Return JSON matching schema', {})
    assert 'response_format' not in payload
    assert payload['chat_template_kwargs'] == {'enable_thinking': False}
    assert payload['max_tokens'] == 4096
    _, _, other = provider.request('another-model', 'Return JSON', {})
    assert other['response_format'] == {'type': 'json_object'}


def test_ultra_still_rejects_invalid_output(monkeypatch):
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_FAST_MODEL', MODEL)
    monkeypatch.setenv('AI_NVIDIA', '1')
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"unexpected":true}'}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(RunFailure, match='INVALID_MODEL_OUTPUT'):
                await Gateway(NVIDIAProvider('test-key'), client=client).suggest('Summarize evidence')

    asyncio.run(run())
    assert len(calls) == 2


def test_evidence_validation_gets_one_correction_without_weakening_rules(monkeypatch):
    import json

    from agent_platform.models import Report
    from agent_platform.runtime import validate_report

    monkeypatch.setenv('AI_PLATFORM_NVIDIA_FAST_MODEL', MODEL)
    monkeypatch.setenv('AI_NVIDIA', '1')
    calls = []
    bad = Report(summary='Test', data_freshness='Saved', historical_context=['Invented history'])
    good = Report(summary='Test', data_freshness='Saved')

    def respond(request):
        calls.append(json.loads(request.content))
        report = bad if len(calls) == 1 else good
        return httpx.Response(200, json={'choices': [{'message': {'content': report.model_dump_json()}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            report, *_ = await Gateway(NVIDIAProvider('test-key'), client=client).report(
                'research', 'Research evidence', {'sources': []}, validate_report)
            assert report.historical_context == []
    asyncio.run(run())
    assert len(calls) == 2
    assert 'UNSUPPORTED_CONTEXT' in calls[1]['messages'][-1]['content']

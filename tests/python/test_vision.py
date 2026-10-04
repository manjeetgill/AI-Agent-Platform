import asyncio
import base64
import io
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from agent_platform import vision
from agent_platform.app import create_app
from agent_platform.tokens import issue


def picture():
    stream = io.BytesIO()
    Image.new('RGB', (100, 100), 'white').save(stream, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()


def test_image_validation():
    assert vision.sanitize_image(picture()).startswith('data:image/jpeg;base64,')
    for value in ['https://example.com/private', 'data:image/png;base64,broken',
                  picture().replace('image/png', 'image/jpeg')]:
        with pytest.raises(HTTPException) as exc:
            vision.sanitize_image(value)
        assert exc.value.status_code == 422


class Repo:
    def __init__(self):
        self.rows = {}
        self.allowed = True

    async def reserve_idea(self, *_):
        return self.allowed

    async def save_vision(self, tenant, user, row):
        self.rows.setdefault((tenant, user), []).append(row)

    async def vision_history(self, tenant, user):
        return self.rows.get((tenant, user), [])


def test_vision_auth_consent_budget_and_private_history(monkeypatch):
    monkeypatch.setenv('AI_VISION', '1')
    monkeypatch.setenv('NVIDIA_API_KEY', 'test')
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_VISION_MODEL', vision.DEFAULT_MODEL)
    report = vision.ImageReport(summary='White square', extracted_facts=['White image'],
                                observations=[], limitations=['Not independently verified'])

    async def fake(*_):
        return report, 10

    monkeypatch.setattr(vision, 'analyze', fake)
    repo = Repo()
    def headers(user='user-a', tenant='test'):
        return {'Authorization': 'Bearer ' + issue('a'*48, issuer='nraialgo', audience='agent-platform',
                tenant=tenant, user=user, scope='agents:access')}
    body = {'image': picture(), 'question': 'Describe this', 'mode': 'chart', 'consent': True}
    with TestClient(create_app(repository=repo, signing_key='a'*48)) as client:
        assert client.post('/v1/agents/vision/analyze', json=body).status_code == 401
        assert client.post('/v1/agents/vision/analyze', headers=headers(), json={**body, 'consent': False}).status_code == 422
        response = client.post('/v1/agents/vision/analyze', headers=headers(), json=body)
        assert response.status_code == 200
        assert 'image' not in response.json()
        assert len(client.get('/v1/agents/vision/reports', headers=headers()).json()['reports']) == 1
        assert client.get('/v1/agents/vision/reports', headers=headers('other')).json()['reports'] == []
        assert client.get('/v1/agents/vision/reports', headers=headers(tenant='other')).json()['reports'] == []
        repo.allowed = False
        assert client.post('/v1/agents/vision/analyze', headers=headers(), json=body).status_code == 429
        assert client.post('/v1/agents/vision/analyze', headers=headers(), content=b'x'*3_000_001).status_code == 413


def test_provider_rejects_truncated_or_invalid_output(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'test')
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_VISION_MODEL', vision.DEFAULT_MODEL)
    body = vision.ImageQuestion(image=picture(), question='Describe this', mode='document', consent=True)
    async def run():
        for finish, content in [('length', '{}'), ('stop', 'not JSON')]:
            def respond(request, finish=finish, content=content):
                payload = json.loads(request.content)
                assert payload['model'] == vision.DEFAULT_MODEL
                assert payload['messages'][1]['content'][1]['type'] == 'image_url'
                return httpx.Response(200, json={'choices': [{'finish_reason': finish, 'message': {'content': content}}]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(HTTPException) as exc:
                    await vision.analyze(body, picture(), client=client)
                assert exc.value.status_code == 502
    asyncio.run(run())

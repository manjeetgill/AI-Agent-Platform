import base64
import json

from agent_platform.meta_client import setup_status


def test_missing_config_does_not_expose_secrets(monkeypatch):
    for key in ('APP_ID', 'CONFIGURATION_ID', 'APP_SECRET', 'VERIFY_TOKEN', 'PUBLIC_URL', 'ENCRYPTION_KEYS', 'ENCRYPTION_KEY_ID'):
        monkeypatch.delenv('AI_META_' + key, raising=False)
    monkeypatch.setenv('AI_META_APP_SECRET', 'private-secret-do-not-display')
    result = setup_status()
    assert result['provider'] == 'meta'
    assert not result['available']
    assert 'configuration_id' in result['missing']
    assert 'private-secret' not in json.dumps(result)


def test_configuration_does_not_claim_public_approval(monkeypatch):
    values = {'APP_ID': '123456', 'CONFIGURATION_ID': '654321', 'APP_SECRET': 's'*32,
        'VERIFY_TOKEN': 'v'*32, 'PUBLIC_URL': 'https://agents.example.com',
        'ENCRYPTION_KEYS': json.dumps({'one': base64.b64encode(b'k'*32).decode()}),
        'ENCRYPTION_KEY_ID': 'one', 'GRAPH_VERSION': 'v26.0'}
    for key, value in values.items():
        monkeypatch.setenv('AI_META_' + key, value)
    result = setup_status()
    assert result['available'] and not result['missing']
    assert result['public_customer_approval'] == 'not_verified'
    monkeypatch.setenv('AI_META_PUBLIC_URL', 'https://[invalid')
    assert not setup_status()['checks']['public_https']

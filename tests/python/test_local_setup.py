"""Local setup preserves private credentials across reruns and reports model readiness."""
import importlib.util
import json
from pathlib import Path

import pytest

from agent_platform.gateway import Gateway, NVIDIAProvider, model_configuration


def test_setup_preserves_private_connection_and_excludes_provider_key(tmp_path, monkeypatch):
    path = Path(__file__).parents[2] / 'scripts/local.py'
    spec = importlib.util.spec_from_file_location('platform_local', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(module, 'CONFIG', tmp_path / 'state/config.json')
    module.setup()
    original = module.load()
    original['NVIDIA_API_KEY'] = 'private-test-provider-key'
    module.save(module.CONFIG, original)
    module.setup()
    assert module.load() == original
    client = json.loads((module.STATE / 'client.json').read_text())
    assert client['AI_PLATFORM_SIGNING_KEY'] == original['AI_PLATFORM_SIGNING_KEY']
    assert 'NVIDIA_API_KEY' not in client
    assert module.CONFIG.stat().st_mode & 0o077 == 0
    module.CONFIG.chmod(0o644)
    with pytest.raises(SystemExit, match='0600'):
        module.load()


def test_model_configuration_and_blank_reasoning_fallback(monkeypatch):
    monkeypatch.setenv('AI_NVIDIA', '1')
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_FAST_MODEL', 'configured-fast')
    monkeypatch.setenv('AI_PLATFORM_NVIDIA_REASONING_MODEL', '')
    monkeypatch.setenv('NVIDIA_API_KEY', '')
    assert model_configuration()['configured'] is False
    monkeypatch.setenv('NVIDIA_API_KEY', 'secret-test-key')
    assert model_configuration()['configured'] is True
    assert 'secret-test-key' not in str(model_configuration())
    assert Gateway(NVIDIAProvider('secret-test-key')).model_for('challenge') == 'configured-fast'

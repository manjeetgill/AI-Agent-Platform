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


@pytest.mark.parametrize('failed', ['api', 'worker'])
def test_local_launcher_stops_companion_and_reports_stale_health(tmp_path, monkeypatch, failed):
    spec = importlib.util.spec_from_file_location('platform_local', Path(__file__).parents[2] / 'scripts/local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE', tmp_path)
    children = []

    class Child:
        def __init__(self, args, **kwargs):
            self.name = 'worker' if args[2] == 'agent_platform.task_worker' else 'api'
            self.stopped = self.name == failed
            children.append(self)

        def poll(self):
            return 1 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout=None):
            assert self.stopped

    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / 'scripts'))
    monkeypatch.setattr(module.subprocess, 'Popen', Child)
    with pytest.raises(SystemExit, match=f'Local {failed} exited'):
        module.supervise({'AI_DURABLE_TASKS': '1'}, {'agent_api': 'postgresql://api/test', 'agent_worker': 'postgresql://worker/test'})
    assert len(children) == 2 and all(child.stopped for child in children)
    assert module.service_status() == {'api': False, 'worker': False}
    module.save(tmp_path / 'services.json', {'updated_at': module.time.time() - 60,
                                           'running': {'api': True, 'worker': True}})
    assert module.service_status() == {'api': False, 'worker': False}


def test_local_launcher_lock_prevents_second_worker(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('platform_local', Path(__file__).parents[2] / 'scripts/local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE', tmp_path)
    with module.lifecycle_lock(), pytest.raises(SystemExit, match='already running'), module.lifecycle_lock():
        pytest.fail('Second launcher acquired lock')

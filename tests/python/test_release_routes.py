"""Release proxy additions must preserve the shared host and private workspace."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'release_orchestration', Path(__file__).resolve().parents[2] / 'deploy/release_orchestration.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_release_routes_preserve_existing_hosts_and_are_idempotent():
    before = 'myalgodesk.in {\n    respond 200\n}\n' \
        'agents.myalgodesk.in, shayak.myalgodesk.in {\n    @businesspages {\n    }\n}\n'
    after = release.notification_connector_routes(before)
    assert after.startswith('myalgodesk.in {\n    respond 200\n}\n')
    assert 'host agents.myalgodesk.in\n        path /v1/connectors' in after
    assert '/v1/notifications/email/*' in after
    assert '/v1/notifications/sms/*' in after
    assert '/console' not in after and '/monitor' not in after
    assert release.notification_connector_routes(after) == after


@pytest.mark.parametrize('source', ['', '    @businesspages {\n    @businesspages {'])
def test_release_routes_reject_unrecognized_layout(source):
    with pytest.raises(RuntimeError, match='Unexpected proxy layout'):
        release.notification_connector_routes(source)


def test_release_upgrades_existing_notification_matcher_once():
    before = """    @platformconnectors {
        host agents.myalgodesk.in
        path /v1/connectors /v1/connectors/* /v1/notifications/email /v1/notifications/email/*
    }
    handle @platformconnectors {
        reverse_proxy agent-platform-api-1:4060
    }
"""
    after = release.notification_connector_routes(before)
    assert after.count('/v1/notifications/sms/*') == 1
    assert after.count('host agents.myalgodesk.in') == 1
    assert release.notification_connector_routes(after) == after
    with pytest.raises(RuntimeError, match='Unexpected proxy layout'):
        release.notification_connector_routes(before.replace('agents.myalgodesk.in', 'another.example'))


def test_voice_routes_keep_internal_control_and_workspace_private():
    before='agents.myalgodesk.in {\n    @businesspages {\n    }\n}\n'
    after=release.voice_routes(before)
    assert '/v1/voice/sessions/*' in after
    assert '/internal/' not in after and '/console' not in after
    assert release.voice_routes(after)==after
    assert '/v1/voice/health' in after
    assert release.voice_routes(after.replace(' /v1/voice/health', '')) == after
    with pytest.raises(RuntimeError):
        release.voice_routes('    @voiceapps {\n    @businesspages {')

"""Fixed protocol v1 interoperability vector; no peer application import."""
import json
from pathlib import Path

import pytest

from agent_platform.tokens import TokenError, verify


def test_delegation_wire_contract():
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/agent_delegation_v1.json').read_text())
    options = {'issuer': 'nraialgo', 'audience': 'agent-platform', 'scope': 'agents:access'}
    assert verify(fixture['secret'], fixture['token'], now=fixture['now'], **options) == fixture['claims']
    with pytest.raises(TokenError):
        verify(fixture['secret'], fixture['token'], now=fixture['claims']['exp'], **options)
    with pytest.raises(TokenError):
        verify(fixture['secret'], fixture['token'] + 'x', now=fixture['now'], **options)

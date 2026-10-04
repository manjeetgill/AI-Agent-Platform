"""Known evidence/report fixtures make provider changes regression-testable."""
import json
from pathlib import Path

from agent_platform.evaluation import evaluate


def test_agent_evaluation_fixtures():
    cases = json.loads((Path(__file__).parents[1] / 'fixtures/agent_platform_eval.json').read_text())
    assert len(cases) >= 3
    for case in cases:
        result = evaluate(case['evidence'], case['output'])
        assert result['valid'] is case['valid'], case['name']
        if case['valid']:
            assert result['facts'] == result['cited_facts']

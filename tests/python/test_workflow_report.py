import asyncio

from agent_platform.workflow_report import assemble_workflow


def test_combined_report_preserves_conflicts_and_owner_scope():
    class Repo:
        async def get_run(self, tenant, user, run_id):
            assert (tenant, user) == ('tenant', 'owner')
            return {'status': 'COMPLETED', 'report': {'summary': 'Supporting' if run_id == '1' else 'Conflicting'},
                    'evidence_refs': [{'instrument': 'NSE:TEST'}]}
    result = asyncio.run(assemble_workflow(Repo(), {'tenant': 'tenant', 'user': 'owner'},
        {'kind': 'signal_review', 'status': 'COMPLETED', 'step_run_ids': ['1', '2']}))
    assert result['complete']
    assert result['completed_steps'] == 2
    assert [step['report']['summary'] for step in result['steps']] == ['Supporting', 'Conflicting']
    assert 'score' not in result


def test_missing_or_failed_steps_do_not_look_complete():
    class Repo:
        async def get_run(self, *_):
            return None
    result = asyncio.run(assemble_workflow(Repo(), {'tenant': 'tenant', 'user': 'owner'},
        {'kind': 'backtest_review', 'status': 'COMPLETED', 'step_run_ids': ['1', '2']}))
    assert not result['complete']
    assert result['completed_steps'] == 0
    assert all(step['error_code'] == 'REPORT_UNAVAILABLE' for step in result['steps'])


def test_unstarted_steps_are_explicit():
    class Repo:
        async def get_run(self, *_):
            raise AssertionError('No run should be read before a step has started')
    result = asyncio.run(assemble_workflow(Repo(), {'tenant': 'tenant', 'user': 'owner'},
        {'kind': 'signal_review', 'status': 'RUNNING', 'step_run_ids': []}))
    assert [step['status'] for step in result['steps']] == ['PENDING', 'PENDING']
    assert not result['complete']

"""Assemble saved workflow evidence without inventing a consensus or numerical score."""
SCOPES = {
    'signal_review': {
        'title': 'Signal investigation', 'steps': ['Signal Researcher', 'Signal Challenger'],
        'limitations': ['Based on one saved scanner signal.',
                        'Historical matching, market/sector context and portfolio risk are not connected.',
                        'No calibrated challenge score or prediction is produced.'],
    },
    'backtest_review': {
        'title': 'Strategy audit', 'steps': ['Backtest Auditor', 'Strategy Doctor'],
        'limitations': ['Based on one completed archive backtest.',
                        'No parameter sweep, multi-strategy comparison or regime study was run.',
                        'One basket does not establish statistical reliability.'],
    },
}


async def assemble_workflow(repo, claims, workflow):
    scope = SCOPES[workflow['kind']]
    steps = []
    for index in range(2):
        if index >= len(workflow['step_run_ids']):
            steps.append({'name': scope['steps'][index], 'run_id': None, 'report': None,
                          'status': 'PENDING' if workflow['status'] in {'CREATED', 'RUNNING'} else 'NOT_RUN',
                          'error_code': None, 'evidence_refs': []})
            continue
        run_id = workflow['step_run_ids'][index]
        row = await repo.get_run(claims['tenant'], claims['user'], str(run_id))
        steps.append({'name': scope['steps'][index], 'run_id': str(run_id),
                      'status': row['status'] if row else 'UNAVAILABLE',
                      'report': row.get('report') if row else None,
                      'error_code': row.get('error_code') if row else 'REPORT_UNAVAILABLE',
                      'evidence_refs': row.get('evidence_refs', []) if row else []})
    completed = sum(step['status'] == 'COMPLETED' and bool(step['report']) for step in steps)
    return {**workflow, 'title': scope['title'], 'steps': steps,
            'complete': workflow['status'] == 'COMPLETED' and completed == 2,
            'completed_steps': completed, 'expected_steps': 2, 'limitations': scope['limitations']}

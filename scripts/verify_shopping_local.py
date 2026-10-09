"""Resumable local model -> catalogue tool -> real worker acceptance check."""
import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from verify_dealagent_live import save

REQUEST = 'Find Samsung TVs under ₹60,000.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-file', type=Path, required=True)
    parser.add_argument('--state-file', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.client_file.read_text())
    if config['url'].rstrip('/') != 'http://127.0.0.1:4060':
        raise SystemExit('This acceptance check uses only the local platform on port 4060')
    state = json.loads(args.state_file.read_text()) if args.state_file.exists() else {
        'app_id': config['app_id'], 'external_user': config['external_user'],
        'request': REQUEST, 'idempotency_key': uuid4().hex}
    if (state['app_id'], state['external_user'], state['request']) != (
            config['app_id'], config['external_user'], REQUEST):
        raise SystemExit('Saved verification belongs to a different request or identity')
    save(args.state_file, state)
    headers = {'Authorization': 'Bearer ' + config['credential'],
               'X-External-User-ID': config['external_user']}

    def request(client, method, path, body=None):
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            raise SystemExit(f'Local API returned {response.status_code}; state retained, no blind retry')
        return response.json()

    with httpx.Client(base_url=config['url'], headers=headers, timeout=50,
                      trust_env=False, follow_redirects=False) as client:
        if 'proposal' not in state:
            state['proposal'] = request(client, 'POST', '/v1/task-agents/interpret', {'request': REQUEST})
            save(args.state_file, state)
        proposal = state['proposal']
        if not proposal.get('ready'):
            raise SystemExit('Planner needs clarification; saved proposal retained for review')
        steps = proposal['blueprint']['steps']
        if len(steps) != 1 or steps[0]['tool'] != 'dealagent.catalog_search':
            raise SystemExit('Planner selected a different action; saved plan needs review before execution')
        if 'agent_id' not in state:
            agent = request(client, 'POST', '/v1/task-agents', {
                'blueprint': proposal['blueprint'], 'idempotency_key': state['idempotency_key']})
            state['agent_id'] = agent['id']
            save(args.state_file, state)
        if 'task_id' not in state:
            task = request(client, 'POST', '/v1/task-agents/' + state['agent_id'] + '/runs',
                           {'idempotency_key': state['idempotency_key']})
            state['task_id'] = task['id']
            save(args.state_file, state)
    # Close the initiating HTTP client completely. Execution must continue in the
    # independent task worker; reconnecting retrieves the same persisted task.
    deadline = time.monotonic() + 90
    with httpx.Client(base_url=config['url'], headers=headers, timeout=10,
                      trust_env=False, follow_redirects=False) as client:
        while True:
            task = request(client, 'GET', '/v1/tasks/' + state['task_id'])
            state.update(status=task['status'], steps=task['steps'])
            save(args.state_file, state)
            if task['status'] not in {'READY', 'RUNNING'} or time.monotonic() >= deadline:
                break
            time.sleep(1)
    print(json.dumps({'task_id': state['task_id'], 'status': state['status'],
                      'verification_scope': 'Model and worker execution only; review product relevance separately.',
                      'usage': proposal.get('usage'), 'steps': state['steps']}, indent=2))
    if state['status'] != 'DONE':
        raise SystemExit('Not completed; rerun with the same state file to inspect the same task')


if __name__ == '__main__':
    main()

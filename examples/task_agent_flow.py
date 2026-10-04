"""Run the read-only decision example against an operator-configured API and worker.

Set AGENT_API_URL, AGENT_SERVICE_KEY and AGENT_EXTERNAL_USER_ID. No credentials are printed.
No SEND or USER_ONLY proposal is run by this example.
"""
import json
import os
import time
from uuid import uuid4

import httpx

REQUEST = ('Create a reusable agent to compare options A and B. Use cost with weight 2 and quality '
           'with weight 1. Higher scores are better. A scores 9 for cost and 6 for quality. '
           'B scores 6 for cost and 9 for quality. Use only these supplied scores.')


def main():
    headers = {'Authorization': 'Bearer '+os.environ['AGENT_SERVICE_KEY'],
               'X-External-User-ID': os.environ['AGENT_EXTERNAL_USER_ID']}
    with httpx.Client(base_url=os.environ['AGENT_API_URL'], headers=headers, timeout=50) as client:
        def post(path, body):
            response = client.post(path, json=body)
            response.raise_for_status()
            return response.json()
        proposal = post('/v1/task-agents/interpret', {'request': REQUEST})
        print(json.dumps(proposal, indent=2))
        if not proposal['ready'] or any(p['risk'] != 'READ' for p in proposal['preview']):
            print('Needs user input or a dedicated approval UI; no agent invocation started.')
            return
        agent = post('/v1/task-agents', {'blueprint': proposal['blueprint'], 'idempotency_key': uuid4().hex})
        task = post(f'/v1/task-agents/{agent["id"]}/runs', {'idempotency_key': uuid4().hex})
        print('Saved agent:', agent['id'], 'Task:', task['id'])
        # Keep these IDs and keys in your application before retrying a lost response.
        cursor, deadline = '0', time.monotonic()+120
        while time.monotonic() < deadline:
            with client.stream('GET', f'/v1/tasks/{task["id"]}/stream', headers={'Last-Event-ID': cursor}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('id: '):
                        cursor = line[4:]
                    if line.startswith('event: auth_required'):
                        raise RuntimeError('Refresh service credentials before reconnecting')
                    if line.startswith('data: '):
                        event = json.loads(line[6:])
                        print(event['event'])
                        if event['event'] in {'DONE', 'FAILED', 'OUTCOME_UNKNOWN', 'WAITING_APPROVAL', 'WAITING_USER'}:
                            result = client.get(f'/v1/tasks/{task["id"]}')
                            result.raise_for_status()
                            print(json.dumps(result.json(), indent=2))
                            return
        print('Task is still saved. Resume observation using its task ID.')


if __name__ == '__main__':
    main()

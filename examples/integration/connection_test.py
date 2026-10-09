"""Backend-only example: python connection_test.py /private/client.json [test-uuid]."""
import json
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx

config = json.loads(Path(sys.argv[1]).read_text())
# In an app, derive this from the authenticated user, never a browser-supplied ID.
headers = {'Authorization': 'Bearer '+config['credential'], 'X-External-User-ID': config['external_user']}
test_id = str(UUID(sys.argv[2])) if len(sys.argv) > 2 else str(uuid4())
print('Test ID (reuse after a timeout):', test_id)
with httpx.Client(base_url=config['url'], headers=headers, timeout=15, follow_redirects=False, trust_env=False) as client:
    readiness = client.get('/v1/integrations/task-readiness', params={'planning':'false','required_tool':'platform.connection_test'})
    readiness.raise_for_status()
    if not readiness.json()['ready']:
        print(json.dumps(readiness.json()['checks'], indent=2))
        raise SystemExit('Resolve the reported prerequisites before testing.')
    # One bounded write. Reuse test_id after uncertainty; do not create another run blindly.
    response = client.post('/v1/integrations/connection-test', json={'test_id':test_id})
    response.raise_for_status()
    task_id = str(UUID(response.json()['task_id']))
    print('Task ID:', task_id)
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        response = client.get('/v1/tasks/'+task_id)
        response.raise_for_status()
        task = response.json()
        if task['status'] in {'DONE','FAILED','CANCELLED','OUTCOME_UNKNOWN','WAITING_USER','WAITING_APPROVAL','PAUSED'}:
            print(json.dumps({'status':task['status'],'steps':task['steps']}, indent=2))
            raise SystemExit(0 if task['status']=='DONE' else 2)
        time.sleep(1)
    raise SystemExit('Still pending: inspect the saved task in the monitor. No duplicate was submitted.')

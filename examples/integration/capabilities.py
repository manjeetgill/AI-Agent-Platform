"""Read-only backend preflight: python capabilities.py /private/client.json.

Derive external_user from the consuming app's authenticated session in production.
No model requests, messages, activation or paid provider probes are performed.
"""
import json
import sys
from pathlib import Path

import httpx


def check(config):
    with httpx.Client(base_url=config['url'], timeout=15, follow_redirects=False, trust_env=False,
                      headers={'Authorization': 'Bearer '+config['credential'],
                               'X-External-User-ID': config['external_user']}) as client:
        response = client.get('/v1/integrations/capability-readiness')
        if response.status_code != 200:
            raise RuntimeError(f'Capability check unavailable (HTTP {response.status_code}); check the backend connection.')
        report = response.json()
    states = {c['id']: c for c in report['capabilities']}
    return [{'capability': c['id'], 'api_root': c['api_root'],
             'missing_scopes': c['missing_scopes'],
             'status': ('ACCESS_REQUIRED' if not c['permissions_granted'] else
                        states.get(c['readiness_id'], {}).get('status', 'CONFIGURATION_CHECK_REQUIRED')),
             'next_step': c['setup_requirement']}
            for c in report['integration_contracts']]


if __name__ == '__main__':
    config = json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(check(config), indent=2))

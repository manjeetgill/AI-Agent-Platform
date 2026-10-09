"""Inspect a local app connection without sending prompts or contacting providers."""
import argparse
import json
import os
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-file', type=Path, required=True)
    parser.add_argument('--tool', action='append', default=[])
    parser.add_argument('--source')
    parser.add_argument('--no-planning', action='store_true', help='Check execution of saved plans only')
    parser.add_argument('--json', action='store_true', help='Print the structured report without credentials')
    args = parser.parse_args()
    path = args.client_file
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise SystemExit('Use an owner-only private client file (permissions 0600).')
    try:
        config = json.loads(path.read_text())
        if config['url'].rstrip('/') not in {'http://127.0.0.1:4060', 'http://localhost:4060'}:
            raise ValueError()
        headers = {'Authorization': 'Bearer ' + config['credential'], 'X-External-User-ID': config['external_user']}
    except (ValueError, KeyError, TypeError):
        raise SystemExit('Client file must contain a local platform URL, credential and external_user.') from None
    params = [('required_tool', tool) for tool in args.tool] + [('planning', 'false' if args.no_planning else 'true')]
    if args.source:
        params.append(('source', args.source))
    try:
        with httpx.Client(base_url=config['url'], headers=headers, timeout=12,
                          trust_env=False, follow_redirects=False) as client:
            response = client.get('/v1/integrations/task-readiness', params=params)
    except httpx.HTTPError:
        raise SystemExit('Local platform unavailable. Start the API and worker with scripts/local.py start.') from None
    if response.status_code != 200:
        messages = {401: 'Credential expired or invalid; ask the operator to rotate it.',
                    403: 'Credential needs tasks:read for readiness checks.',
                    422: 'Check required tool/source arguments and external user.',
                    503: 'Database or migrations unavailable; ask the operator to check local startup.'}
        raise SystemExit(messages.get(response.status_code, f'Local API returned HTTP {response.status_code}.'))
    report = response.json()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print('Application readiness: ' + report['status'])
        for check in report['checks']:
            print(f"[{check['status']}] {check['summary']}")
            if check['next_action']:
                print('  ' + check['next_action'])
        print('No model or provider requests were made.')
    return 0 if report['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())

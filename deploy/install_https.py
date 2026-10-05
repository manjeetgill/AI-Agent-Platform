"""Root-only pilot proxy overlay. Run again after a main-app release.

Keeps immutable main-app source untouched. Validates before replacement and
restores the prior proxy configuration if the main app fails its health check.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    root = Path('/opt/myalgodesk')
    target = Path('/opt/agent-platform/proxy')
    with (root / 'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = (root / 'state/current-release').read_text().strip()
        source = root / 'releases' / release / 'deploy/vps'
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        combined = target / 'Caddyfile'
        # Both Compose projects advertise the alias api on the shared proxy.
        main_config = (source / 'Caddyfile').read_text().replace(
            'reverse_proxy api:4020', 'reverse_proxy myalgodesk-api-1:4020')
        combined.write_text(main_config + '\n' +
                            Path(__file__).with_name('agents.Caddyfile').read_text())
        overlay = target / 'compose.json'
        overlay.write_text(json.dumps({'services': {'proxy': {
            'networks': ['application', 'agent-edge'],
            'volumes': [{'type': 'bind', 'source': str(combined), 'target': '/etc/caddy/Caddyfile',
                         'read_only': True}]}},
            'networks': {'agent-edge': {'external': True, 'name': 'agent-platform-edge'}}}))
        image = subprocess.check_output(['docker', 'inspect', 'myalgodesk-proxy-1',
                                         '--format', '{{.Config.Image}}'], text=True).strip()
        run(['docker', 'run', '--rm', '--network', 'agent-platform-edge',
             '-e', 'APP_DOMAIN=myalgodesk.in', '-v', f'{combined}:/etc/caddy/Caddyfile:ro',
             image, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile'])
        env = {**os.environ, 'RELEASE_TAG': release, 'VPS_STATE_DIR': str(root/'state'),
               'APP_DOMAIN': 'myalgodesk.in'}
        base = ['docker', 'compose', '-p', 'myalgodesk', '-f', str(source/'compose.yaml'),
                '-f', str(root/'state/compose.runtime-data.json')]
        args = ['up', '-d', '--no-deps', '--no-build', 'proxy']
        try:
            run([*base, '-f', str(overlay), *args], env=env)
            # Caddy admin is disabled; an in-place mounted file change needs restart.
            run(['docker', 'restart', 'myalgodesk-proxy-1'])
            for _ in range(20):
                try:
                    with urllib.request.urlopen('https://myalgodesk.in/login', timeout=4) as r:
                        if r.status == 200:
                            print('Main app healthy; agent HTTPS route installed.')
                            return
                except OSError:
                    pass
                time.sleep(2)
            raise RuntimeError('Main app health check failed')
        except Exception:
            run([*base, *args], env=env)
            raise


if __name__ == '__main__':
    main()

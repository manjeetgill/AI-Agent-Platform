"""Surgical pilot update; stage the two Meta modules alongside this script.

Run as root on the existing pilot. Backs up configuration privately, validates
Caddy before changing it, and rolls back configuration on failed health checks.
No model keys, customer tokens, schemas or existing number bindings are changed.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def run(*args):
    return subprocess.run(args, check=True)


def status(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration-id', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r'\d{5,30}', args.configuration_id):
        raise SystemExit('Requires root and a numeric Meta configuration ID')
    stage = Path(__file__).resolve().parent
    root = Path('/opt/agent-platform/pilot')
    combined = Path('/opt/agent-platform/proxy/Caddyfile')
    source = root / 'deploy/agents.Caddyfile'
    overlay = root / 'deploy/compose.internet-voice.json'
    environment = root / '.env'
    compose = ['docker', 'compose', '--project-directory', str(root), '-p', 'agent-platform',
               '-f', str(root/'compose.yaml'), '-f', str(root/'deploy/compose.small.yaml'), '-f', str(overlay)]
    with Path('/opt/myalgodesk/release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        backup = Path('/opt/agent-platform/secrets') / ('whatsapp-integration-' + stamp)
        backup.mkdir(mode=0o700)
        targets = [combined, source, overlay, environment]
        for index, target in enumerate(targets):
            shutil.copy2(target, backup/str(index))
        current = combined.read_text()
        marker = 'agents.myalgodesk.in, shayak.myalgodesk.in {'
        if current.count(marker) != 1 or '@whatsappapps' in current:
            raise SystemExit('Unexpected proxy layout; inspect before updating')
        main_config, agent_config = current.split(marker)
        routes = '''    # Backend-only service credentials are checked on every API request.
    @whatsappapps {
        host agents.myalgodesk.in
        path /v1/whatsapp/* /v1/knowledge/documents /v1/knowledge/documents/*
    }
    handle @whatsappapps {
        reverse_proxy agent-platform-api-1:4060
    }
'''
        if agent_config.count('    @public path ') != 1:
            raise SystemExit('Unexpected public matcher; inspect before updating')
        agent_config = marker + agent_config.replace('    @public path ', routes+'    @public path ', 1)
        candidate = stage/'Caddyfile.candidate'
        candidate.write_text(main_config + agent_config)
        proxy_image = subprocess.check_output(['docker', 'inspect', 'myalgodesk-proxy-1',
                                               '--format', '{{.Config.Image}}'], text=True).strip()
        run('docker', 'run', '--rm', '--network', 'agent-platform-edge', '-e', 'APP_DOMAIN=myalgodesk.in',
            '-v', f'{candidate}:/etc/caddy/Caddyfile:ro', proxy_image, 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile')
        old_image = subprocess.check_output(['docker', 'inspect', 'agent-platform-api-1',
                                             '--format', '{{.Config.Image}}'], text=True).strip()
        new_image = 'agent-platform:whatsapp-integration-' + stamp.lower()
        package = '/usr/local/lib/python3.14/site-packages/agent_platform/'
        (stage/'Dockerfile').write_text(f'FROM {old_image}\nCOPY meta_client.py meta_onboarding.py {package}\n')
        run('docker', 'build', '--network=none', '-t', new_image, str(stage))
        run('docker', 'run', '--rm', '--network=none', new_image, 'python', '-c',
            'from agent_platform.meta_client import setup_status; from agent_platform.meta_onboarding import install_onboarding; assert callable(setup_status)')
        try:
            env = environment.read_text()
            entry = 'AI_META_CONFIGURATION_ID=' + args.configuration_id
            env = re.sub(r'^AI_META_CONFIGURATION_ID=.*$', entry, env, flags=re.MULTILINE) if re.search(r'^AI_META_CONFIGURATION_ID=', env, re.MULTILINE) else env+'\n'+entry+'\n'
            environment.write_text(env)
            data = json.loads(overlay.read_text())
            data['services']['api']['image'] = new_image
            overlay.write_text(json.dumps(data, indent=2)+'\n')
            run(*compose, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'api', 'meta-worker')
            for _ in range(20):
                if status('http://127.0.0.1:4060/readiness') == 200:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Updated API did not become ready')
            # Write in place: Docker binds this file's inode.
            combined.write_text(candidate.read_text())
            source.write_text(agent_config)
            run('docker', 'restart', 'myalgodesk-proxy-1')
            expected = {
                'https://myalgodesk.in/login': 200,
                'https://agents.myalgodesk.in/v1/whatsapp/onboarding/config': 401,
                'https://agents.myalgodesk.in/v1/whatsapp/setup': 401,
                'https://agents.myalgodesk.in/v1/knowledge/documents': 401,
                'https://agents.myalgodesk.in/console/api/whatsapp/setup': 404,
                'https://agents.myalgodesk.in/openapi.json': 404,
                'https://shayak.myalgodesk.in/v1/whatsapp/setup': 404,
            }
            for _ in range(10):
                observed = {url: status(url) for url in expected}
                if observed == expected:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Proxy verification failed: '+json.dumps(observed))
            # Keep source deployments consistent with the patched runtime.
            for name in ('meta_client.py', 'meta_onboarding.py'):
                target = root/'src/agent_platform'/name
                shutil.copy2(target, backup/name)
                shutil.copy2(stage/name, target)
            print(json.dumps({'image': new_image, 'backup': str(backup), 'checks': observed}))
        except Exception:
            for index, target in enumerate(targets):
                target.write_bytes((backup/str(index)).read_bytes())
            run(*compose, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'api', 'meta-worker')
            run('docker', 'restart', 'myalgodesk-proxy-1')
            raise


if __name__ == '__main__':
    main()

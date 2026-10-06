"""Publish only the reviewed business pages on the existing agent pilot.

Stage public_pages.py, meta_onboarding.py and public/ alongside this script.
Run as root. Preserves the current API image as its base, holds the release lock,
validates proxy configuration and restores runtime configuration on failure.
"""
import fcntl
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def run(*args):
    subprocess.run(args, check=True)


def status(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the pilot')
    stage = Path(__file__).resolve().parent
    root = Path('/opt/agent-platform/pilot')
    combined = Path('/opt/agent-platform/proxy/Caddyfile')
    source = root/'deploy/agents.Caddyfile'
    overlay = root/'deploy/compose.internet-voice.json'
    compose = ['docker', 'compose', '--project-directory', str(root), '-p', 'agent-platform',
               '-f', str(root/'compose.yaml'), '-f', str(root/'deploy/compose.small.yaml'), '-f', str(overlay)]
    with Path('/opt/myalgodesk/release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        backup = Path('/opt/agent-platform/secrets')/('public-pages-'+stamp)
        backup.mkdir(mode=0o700)
        targets = [combined, source, overlay]
        for index, target in enumerate(targets):
            shutil.copy2(target, backup/str(index))
        current = combined.read_text()
        marker = 'agents.myalgodesk.in, shayak.myalgodesk.in {'
        if current.count(marker) != 1 or '@businesspages' in current:
            raise SystemExit('Unexpected proxy layout; inspect before updating')
        main_config, agent_config = current.split(marker)
        routes = '''    @businesspages {
        host agents.myalgodesk.in
        path /about /privacy /data-deletion /app-icon.png
    }
    handle @businesspages {
        reverse_proxy agent-platform-api-1:4060
    }
'''
        if agent_config.count('    @public path ') != 1:
            raise SystemExit('Unexpected public matcher')
        agent_config = marker+agent_config.replace('    @public path ', routes+'    @public path ', 1)
        candidate = stage/'Caddyfile.candidate'
        candidate.write_text(main_config+agent_config)
        proxy_image = subprocess.check_output(['docker','inspect','myalgodesk-proxy-1','--format','{{.Config.Image}}'],text=True).strip()
        run('docker','run','--rm','--network','agent-platform-edge','-e','APP_DOMAIN=myalgodesk.in',
            '-v',f'{candidate}:/etc/caddy/Caddyfile:ro',proxy_image,'caddy','validate','--config','/etc/caddy/Caddyfile')
        old_image = subprocess.check_output(['docker','inspect','agent-platform-api-1','--format','{{.Config.Image}}'],text=True).strip()
        new_image = 'agent-platform:public-pages-'+stamp.lower()
        package='/usr/local/lib/python3.14/site-packages/agent_platform/'
        (stage/'Dockerfile').write_text(f'FROM {old_image}\nCOPY public_pages.py meta_onboarding.py {package}\nCOPY public/ {package}public/\n')
        run('docker','build','--network=none','-t',new_image,str(stage))
        run('docker','run','--rm','--network=none',new_image,'python','-c',
            'from agent_platform.public_pages import ASSETS; from agent_platform.meta_onboarding import install_onboarding; assert (ASSETS/"privacy.html").is_file()')
        try:
            data=json.loads(overlay.read_text());data['services']['api']['image']=new_image
            overlay.write_text(json.dumps(data,indent=2)+'\n')
            run(*compose,'up','-d','--no-deps','--no-build','--pull','never','api')
            for _ in range(20):
                if status('http://127.0.0.1:4060/readiness') == 200:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('API readiness failed')
            combined.write_text(candidate.read_text());source.write_text(agent_config)
            run('docker','restart','myalgodesk-proxy-1')
            expected={
                'https://myalgodesk.in/login':200,
                'https://agents.myalgodesk.in/about':200,
                'https://agents.myalgodesk.in/privacy':200,
                'https://agents.myalgodesk.in/data-deletion':200,
                'https://agents.myalgodesk.in/app-icon.png':200,
                'https://agents.myalgodesk.in/v1/whatsapp/setup':401,
                'https://agents.myalgodesk.in/console/api/whatsapp/setup':404,
                'https://agents.myalgodesk.in/openapi.json':404,
                'https://shayak.myalgodesk.in/privacy':404,
            }
            for _ in range(10):
                observed={url:status(url) for url in expected}
                if observed == expected:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Route verification failed: '+json.dumps(observed))
            for name in ('meta_onboarding.py','public_pages.py'):
                target=root/'src/agent_platform'/name
                if target.exists():
                    shutil.copy2(target,backup/name)
                shutil.copy2(stage/name,target)
            shutil.copytree(stage/'public',root/'src/agent_platform/public',dirs_exist_ok=True)
            print(json.dumps({'image':new_image,'backup':str(backup),'checks':observed}))
        except Exception:
            for index,target in enumerate(targets):
                target.write_bytes((backup/str(index)).read_bytes())
            run(*compose,'up','-d','--no-deps','--no-build','--pull','never','api')
            run('docker','restart','myalgodesk-proxy-1')
            raise


if __name__ == '__main__':
    main()

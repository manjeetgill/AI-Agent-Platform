"""Release platform code on the existing pilot, retaining live service configuration.

Run from a root-owned staged git archive. Credentials and backups stay outside the
build context. Only this Compose project's five application services are recreated.
"""
import fcntl
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVICES = ['api', 'worker', 'whatsapp-worker', 'meta-worker', 'voice-worker']


def capture(args):
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('Deployment command failed: '+args[0]+' '+args[1])
    return result.stdout


def inspect(name):
    return json.loads(capture(['docker', 'inspect', 'agent-platform-'+name+'-1']))[0]


def private(path, data):
    with open(path, 'w', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(data)
    path.chmod(0o600)


def status(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def wait_ready():
    for _ in range(30):
        if status('http://127.0.0.1:4060/readiness') == 200:
            return
        time.sleep(2)
    raise RuntimeError('API did not become ready')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run as root on the existing pilot')
    stage = Path(__file__).resolve().parents[1]
    revision = (stage/'REVISION').read_text().strip()
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise SystemExit('Staged revision is required')
    with Path('/opt/myalgodesk/release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        live = {name: inspect(name) for name in SERVICES}
        labels = live['api']['Config']['Labels']
        files = labels['com.docker.compose.project.config_files'].split(',')
        compose = ['docker', 'compose', '--profile', '*', '-p', 'agent-platform']
        for filename in files:
            compose += ['-f', filename]
        config = json.loads(capture(compose+['config', '--format', 'json']))
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        backup = Path('/opt/agent-platform/secrets')/('release-'+stamp)
        backup.mkdir(mode=0o700)
        # Capture actual running images/env, which can differ from old overlay files.
        for name, container in live.items():
            service = config['services'][name]
            service['image'] = container['Config']['Image']
            service['environment'] = dict(entry.split('=', 1) for entry in container['Config']['Env'])
            service.pop('build', None)
        private(backup/'rollback.compose.json', json.dumps(config))
        dump = backup/'database.dump'
        with open(dump, 'wb', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            subprocess.run(['docker','exec','agent-platform-db-1','pg_dump','-U','postgres','-d','agent_platform','-Fc'], stdout=stream, check=True)
        proxy = Path('/opt/agent-platform/proxy/Caddyfile')
        private(backup/'Caddyfile', proxy.read_text())
        build = stage/'release-build'
        build.mkdir()
        import shutil
        shutil.copytree(stage/'src/agent_platform', build/'agent_platform')
        for path in build.rglob('*'):
            path.chmod(0o755 if path.is_dir() else 0o644)
        image = 'agent-platform:release-'+revision[:12]
        (build/'Dockerfile').write_text('FROM '+live['api']['Config']['Image']+'\n'
            'COPY agent_platform/ /usr/local/lib/python3.14/site-packages/agent_platform/\n'
            'LABEL org.opencontainers.image.revision="'+revision+'"\n')
        subprocess.run(['docker','build','--network=none','-t',image,str(build)],check=True)
        capture(['docker','run','--rm','--network=none',image,'python','-c',
                 'from agent_platform import app, automation, adaptive_runtime, calling_worker, whatsapp_worker; import aiortc; print("imports valid")'])
        env = dict(config['services']['api']['environment'])
        env['AI_AUTOMATIONS'] = '1'
        if any('\n' in k or '\n' in v for k,v in env.items()):
            raise RuntimeError('Multiline environment requires separate handling')
        private(backup/'migration.env', '\n'.join(k+'='+v for k,v in env.items())+'\n')
        # Migration driver checks the old manifest and applies pending changes in one transaction.
        capture(['docker','run','--rm','--network','agent-platform_default','--env-file',str(backup/'migration.env'),image,
                 'python','-m','agent_platform.migrate'])
        for name in SERVICES:
            config['services'][name]['image'] = image
        for name in ['api','worker']:
            config['services'][name]['environment']['AI_AUTOMATIONS'] = '1'
        for key,value in env.items():
            if key == 'NVIDIA_API_KEY' or key.startswith(('AI_PLATFORM_NVIDIA_', 'AI_PLATFORM_FALLBACK_', 'AI_MODEL_')):
                config['services']['worker']['environment'][key] = value
        config['services']['migrate']['image'] = image
        config['services']['migrate']['environment'] = env
        config['services']['migrate'].pop('build', None)
        release_config = backup/'release.compose.json'
        private(release_config,json.dumps(config))
        runner = ['docker','compose','-p','agent-platform','-f',str(release_config)]
        capture(runner+['config','--quiet'])
        changed_proxy = False
        try:
            capture(runner+['up','-d','--no-deps','--no-build','--pull','never',*SERVICES])
            wait_ready()
            current = proxy.read_text()
            marker = '    @businesspages {'
            route = '''    @orchestrationapps {
        host agents.myalgodesk.in
        path /v1/task-agents /v1/task-agents/* /v1/tasks /v1/tasks/* /v1/orchestration/* /v1/integrations/*
    }
    handle @orchestrationapps {
        reverse_proxy agent-platform-api-1:4060
    }
'''
            if '@orchestrationapps' not in current:
                if current.count(marker) != 1:
                    raise RuntimeError('Unexpected proxy layout')
                candidate = backup/'Caddyfile.candidate'
                private(candidate,current.replace(marker,route+marker))
                proxy_image=json.loads(capture(['docker','inspect','myalgodesk-proxy-1']))[0]['Config']['Image']
                capture(['docker','run','--rm','--network','agent-platform-edge','-e','APP_DOMAIN=myalgodesk.in',
                         '-v',str(candidate)+':/etc/caddy/Caddyfile:ro',proxy_image,'caddy','validate','--config','/etc/caddy/Caddyfile'])
                proxy.write_text(candidate.read_text())  # Preserve mounted inode.
                changed_proxy = True
                capture(['docker','restart','myalgodesk-proxy-1'])
            expected={'https://myalgodesk.in/login':200,
                      'https://agents.myalgodesk.in/v1/task-agents/tools':401,
                      'https://agents.myalgodesk.in/v1/orchestration/automations':401,
                      'https://agents.myalgodesk.in/v1/whatsapp/setup':401,
                      'https://agents.myalgodesk.in/openapi.json':404,
                      'https://agents.myalgodesk.in/console':404,
                      'https://shayak.myalgodesk.in/v1/orchestration/automations':404}
            for _ in range(10):
                observed={url:status(url) for url in expected}
                if observed==expected:
                    break
                time.sleep(2)
            else:
                raise RuntimeError('HTTPS route verification failed: '+json.dumps(observed))
            private(backup/'result.json',json.dumps({'revision':revision,'image':image,'checks':observed}))
            private(Path('/opt/agent-platform/current-release.json'),json.dumps({'revision':revision,'compose':str(release_config),'backup':str(backup),'source':str(stage)}))
            print(json.dumps({'deployed':True,'revision':revision,'image':image,'backup':str(backup),'checks':observed}))
        except Exception:
            # Additive migrations stay in place; never restore data over live writes.
            capture(['docker','compose','-p','agent-platform','-f',str(backup/'rollback.compose.json'),
                     'up','-d','--no-deps','--no-build','--pull','never',*SERVICES])
            if changed_proxy:
                proxy.write_text((backup/'Caddyfile').read_text())
                capture(['docker','restart','myalgodesk-proxy-1'])
            raise


if __name__ == '__main__':
    main()

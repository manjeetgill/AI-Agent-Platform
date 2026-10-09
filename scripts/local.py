"""Local platform lifecycle. Own database and private configuration; no consumer imports."""
import argparse
import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime' / 'local'
CONFIG = STATE / 'config.json'


def save(path, value):
    if path.is_symlink():
        raise SystemExit('Refusing a symlinked configuration file')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2)
        stream.write('\n')


def load():
    if CONFIG.is_symlink() or not CONFIG.is_file() or CONFIG.stat().st_uid != os.getuid() or CONFIG.stat().st_mode & 0o077:
        raise SystemExit('Run setup first; configuration must have permissions 0600')
    return json.loads(CONFIG.read_text())


def setup():
    if STATE.is_symlink() or STATE.parent.is_symlink():
        raise SystemExit('Refusing a symlinked state directory')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not CONFIG.exists():
        save(CONFIG, {
            'AI_PLATFORM_DATABASE_URL': f'postgresql://agent_platform:{secrets.token_hex(32)}@127.0.0.1:55460/postgres',
            'AI_PLATFORM_SIGNING_KEY': secrets.token_hex(32),
            'AI_PLATFORM_CONNECTOR_URL': 'http://127.0.0.1:4022',
            'NVIDIA_API_KEY': '', 'AI_PLATFORM_NVIDIA_FAST_MODEL': '',
            'AI_PLATFORM_NVIDIA_REASONING_MODEL': '', 'AI_WORKFLOWS': '1',
        })
    values = load()
    save(STATE / 'client.json', {'AI_PLATFORM_ENABLED': '1',
        'AI_PLATFORM_URL': 'http://127.0.0.1:4060',
        'AI_PLATFORM_SIGNING_KEY': values['AI_PLATFORM_SIGNING_KEY']})
    print('Local platform configuration ready. Existing settings preserved.')
    print(f'Consumer connection file: {STATE / "client.json"}')


def pg(name):
    configured = os.environ.get('PG_BIN')
    path = Path(configured or '/opt/homebrew/opt/postgresql@17/bin') / name
    found = str(path) if path.is_file() else shutil.which(name)
    if not found:
        raise SystemExit('Install PostgreSQL 17 or set PG_BIN to its bin directory')
    return found


def database(values):
    from urllib.parse import urlparse
    data = STATE / 'postgres'
    if data.is_symlink():
        raise SystemExit('Refusing a symlinked database directory')
    status = subprocess.run([pg('pg_ctl'), '-D', str(data), 'status'], capture_output=True, check=False)
    if status.returncode == 0:
        return
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 55460)) == 0:
            raise SystemExit('Port 55460 is occupied by another database; nothing changed')
    if not (data / 'PG_VERSION').exists():
        password_file = STATE / 'init-password'
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(urlparse(values['AI_PLATFORM_DATABASE_URL']).password + '\n')
            subprocess.run([pg('initdb'), '-D', str(data), '-U', 'agent_platform',
                '--auth=scram-sha-256', '--pwfile=' + str(password_file)], check=True, stdout=subprocess.DEVNULL)
        finally:
            password_file.unlink(missing_ok=True)
    subprocess.run([pg('pg_ctl'), '-D', str(data), '-l', str(STATE / 'postgres.log'),
                    '-o', '-h 127.0.0.1 -p 55460', '-w', 'start'], check=True)


def nvidia():
    values = load()
    if not sys.stdin.isatty():
        raise SystemExit('Run this command in your Terminal; the API key is entered privately')
    key = getpass.getpass('NVIDIA API key (hidden): ').strip()
    model = input('NVIDIA model ID: ').strip()
    reasoning = input('Reasoning model ID (Enter to use the same model): ').strip() or model
    if not key or not model or any(c.isspace() for c in key + model + reasoning):
        raise SystemExit('Key and model IDs must be nonempty and contain no whitespace; nothing saved')
    values.update(NVIDIA_API_KEY=key, AI_PLATFORM_NVIDIA_FAST_MODEL=model,
                  AI_PLATFORM_NVIDIA_REASONING_MODEL=reasoning)
    save(CONFIG, values)
    print('NVIDIA configuration saved privately. Restart the platform to apply it.')
    print('Provider access will be verified by the first real agent run.')


@contextmanager
def lifecycle_lock():
    """One local launcher; never kill processes belonging to another session."""
    import fcntl
    descriptor = os.open(STATE / 'services.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('The local platform launcher is already running') from None
        yield
    finally:
        os.close(descriptor)


def supervise(env):
    """Stop the companion if either API or worker exits; leave saved tasks intact."""
    children = {}
    try:
        children['api'] = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'agent_platform.app:app',
            '--host', '127.0.0.1', '--port', '4060', '--no-access-log'], env=env, cwd=ROOT)
        if env.get('AI_DURABLE_TASKS') == '1':
            children['worker'] = subprocess.Popen([sys.executable, '-m', 'agent_platform.task_worker'],
                                                 env=env, cwd=ROOT)
        while True:
            statuses = {name: child.poll() is None for name, child in children.items()}
            save(STATE / 'services.json', {'updated_at': time.time(), 'running': statuses})
            if not all(statuses.values()):
                failed = next(name for name, running in statuses.items() if not running)
                raise SystemExit(f'Local {failed} exited. Companion stopped; saved tasks remain. Restart with scripts/local.py start.')
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        save(STATE / 'services.json', {'updated_at': time.time(),
                                     'running': {name: False for name in children}})


def service_status():
    try:
        record = json.loads((STATE / 'services.json').read_text())
        fresh = 0 <= time.time() - record['updated_at'] < 5
        return {name: fresh and record['running'].get(name, False) for name in ('api', 'worker')}
    except (OSError, ValueError, KeyError, TypeError):
        return {'api': False, 'worker': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'start', 'status', 'nvidia', 'vision', 'stop-db'])
    args = parser.parse_args()
    if args.command == 'setup':
        setup()
        return
    values = load()
    if args.command == 'nvidia':
        nvidia()
    elif args.command == 'vision':
        values['AI_VISION'] = '1'
        values['AI_PLATFORM_NVIDIA_VISION_MODEL'] = 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning'
        save(CONFIG, values)
        print('Vision configured using the existing NVIDIA key. Restart the platform to apply it.')
    elif args.command == 'status':
        print('NVIDIA: ' + ('configured (not verified)' if values.get('NVIDIA_API_KEY') and
              values.get('AI_PLATFORM_NVIDIA_FAST_MODEL') else 'awaiting key and model ID'))
        try:
            with urlopen('http://127.0.0.1:4060/readiness', timeout=3) as response:
                print('API/database: ' + json.load(response)['status'])
        except (URLError, TimeoutError):
            print('API/database: stopped or not ready')
        print('Local supervisor: ' + ', '.join(name + (' running' if running else ' not confirmed')
              for name, running in service_status().items()))
    elif args.command == 'stop-db':
        subprocess.run([pg('pg_ctl'), '-D', str(STATE / 'postgres'), '-m', 'fast', 'stop'], check=True)
    else:
        # Local settings are explicit; do not inherit another app's provider key or model URL.
        env = {k: v for k, v in os.environ.items() if not k.startswith(('AI_', 'NVIDIA_'))}
        env.update(values)
        env['PYTHONPATH'] = str(ROOT / 'src')
        with lifecycle_lock():
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', 4060)) == 0:
                    raise SystemExit('Port 4060 is occupied; stop its existing launcher first. Nothing changed.')
            database(values)
            subprocess.run([sys.executable, '-m', 'agent_platform.migrate'], env=env, cwd=ROOT, check=True)
            print('Platform: http://127.0.0.1:4060. Ctrl-C stops API and worker; database and reports persist.', flush=True)
            supervise(env)


if __name__ == '__main__':
    main()

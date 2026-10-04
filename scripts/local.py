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
                print('Platform: ' + json.load(response)['status'])
        except (URLError, TimeoutError):
            print('Platform: stopped or not ready')
    elif args.command == 'stop-db':
        subprocess.run([pg('pg_ctl'), '-D', str(STATE / 'postgres'), '-m', 'fast', 'stop'], check=True)
    else:
        database(values)
        # Local settings are explicit; do not inherit another app's provider key or model URL.
        env = {k: v for k, v in os.environ.items() if not k.startswith(('AI_', 'NVIDIA_'))}
        env.update(values)
        env['PYTHONPATH'] = str(ROOT / 'src')
        subprocess.run([sys.executable, '-m', 'agent_platform.migrate'], env=env, cwd=ROOT, check=True)
        print('Platform: http://127.0.0.1:4060. Ctrl-C stops the API; database and reports persist.', flush=True)
        try:
            subprocess.run([sys.executable, '-m', 'uvicorn', 'agent_platform.app:app',
                '--host', '127.0.0.1', '--port', '4060', '--no-access-log'], env=env, cwd=ROOT, check=True)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()

"""Start the local console and worker without modifying the existing local configuration."""
import asyncio
import json
import os
import subprocess

import uvicorn
from local import ROOT, STATE, database, load
from runtime_env import DATABASE_KEYS, process_env

values = load()
database(values)
os.environ.update(values)
os.environ['PYTHONPATH'] = str(ROOT / 'src')  # Child migrations must use this checkout too.
os.environ.update(AI_DURABLE_TASKS='1', AI_TASK_AGENTS='1')
os.environ.setdefault('AI_WEB_VOICE_ENABLED', '1')
os.environ.setdefault('AI_WHATSAPP_CONNECTIONS_FILE', str(ROOT/'.runtime/local/whatsapp-connections.json'))
import sys

sys.path.insert(0, str(ROOT/'src'))
from agent_platform.calling_worker import serve as serve_calling
from agent_platform.console_app import create_console
from agent_platform.decision_tool import default_registry
from agent_platform.memory_worker import serve as serve_memory
from agent_platform.meta_worker import serve as serve_meta
from agent_platform.task_worker import serve
from agent_platform.whatsapp_worker import serve as serve_whatsapp


async def main():
    roles_file=STATE/'console-database-roles.json'
    if not roles_file.is_file() or roles_file.is_symlink() or roles_file.stat().st_mode & 0o077:
        raise SystemExit('Run scripts/local.py roles, then roles --console; private console role file required')
    roles=json.loads(roles_file.read_text())
    await asyncio.to_thread(subprocess.run,[sys.executable,'-m','agent_platform.migrate'],
                   env=process_env(dict(os.environ),roles['agent_migrator']),check=True)
    # This loopback-only operator process runs distinct API/worker pools. No
    # administrator or migrator credential remains in its runtime environment.
    for name in DATABASE_KEYS:
        os.environ.pop(name,None)
    os.environ.update(AI_PLATFORM_DATABASE_URL=roles['agent_api'],
                      AI_PLATFORM_API_DATABASE_URL=roles['agent_api'],
                      AI_PLATFORM_WORKER_DATABASE_URL=roles['agent_worker'],
                      AI_PLATFORM_OPERATOR_DATABASE_URL=roles['agent_worker'],AI_BROWSER_TASKS='1')
    registry = default_registry()
    server = uvicorn.Server(uvicorn.Config(create_console(registry=registry), host='127.0.0.1', port=4061,
                                           access_log=False))
    worker = asyncio.create_task(serve(default_registry()))
    whatsapp_worker = asyncio.create_task(serve_whatsapp())
    meta_worker = asyncio.create_task(serve_meta())
    calling_worker = asyncio.create_task(serve_calling())
    memory_worker = asyncio.create_task(serve_memory())
    try:
        await server.serve()
    finally:
        worker.cancel()
        whatsapp_worker.cancel()
        meta_worker.cancel()
        calling_worker.cancel()
        memory_worker.cancel()
        await asyncio.gather(worker, whatsapp_worker, meta_worker, calling_worker, memory_worker, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())

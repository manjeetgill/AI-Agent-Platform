"""Platform-owned browser jobs: no end-user device, profile or extension required."""
import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from .automation import scope
from .connector_health import ProbeResult
from .retailer_browser import Capture, as_probe, job_allowed


def runtime_configured():
    node = Path(os.environ.get('AI_RETAILER_NODE', ''))
    module = Path(os.environ.get('AI_RETAILER_PLAYWRIGHT_MODULE', ''))
    return (node.is_absolute() and node.is_file() and os.access(node, os.X_OK)
            and module.is_absolute() and module.is_file())


async def render(job,authorized):
    node=Path(os.environ.get('AI_RETAILER_NODE',''))
    module=Path(os.environ.get('AI_RETAILER_PLAYWRIGHT_MODULE',''))
    if not runtime_configured():
        return ProbeResult(status='CONFIGURATION_REQUIRED',reason_code='server_browser_runtime_missing')
    try:
        hosts=json.loads(os.environ.get('AI_RETAILER_RESOURCE_HOSTS','{}')).get(job['source'],[])
        if not isinstance(hosts,list) or len(hosts)>16:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        return ProbeResult(status='CONFIGURATION_REQUIRED',reason_code='server_browser_host_config_invalid')
    command={'id':str(job['id']),'operation':'retailer.observe','expires_at':job['expires_at'].isoformat(),**job['arguments']}
    process=await asyncio.create_subprocess_exec(str(node),str(Path(__file__).with_name('probes')/'server-retailer.mjs'),
        str(module),json.dumps(command),json.dumps(hosts),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
        # No app/provider credentials, user profile or proxy settings in the renderer.
        env={'PATH':str(node.parent),'HOME':os.environ.get('HOME',''),
             **({'PLAYWRIGHT_BROWSERS_PATH':os.environ['PLAYWRIGHT_BROWSERS_PATH']} if os.environ.get('PLAYWRIGHT_BROWSERS_PATH') else {})},limit=16000)
    async def read():
        value=bytearray()
        while part:=await process.stdout.read(2048):
            value.extend(part)
            if len(value)>12000:raise ValueError('Bound exceeded')
        return bytes(value)
    reader=asyncio.create_task(read())
    try:
        async with asyncio.timeout(50):
            while not reader.done():
                await asyncio.wait({reader},timeout=0.25)
                if not await authorized():
                    return ProbeResult(status='CONFIGURATION_REQUIRED',reason_code='server_browser_revoked')
            payload=json.loads(await reader)
            await process.wait()
            if process.returncode:raise ValueError()
            if payload=={'configuration_required':True}:
                return ProbeResult(status='CONFIGURATION_REQUIRED',reason_code='server_browser_runtime_unavailable')
            return as_probe(job,Capture.model_validate(payload,strict=True))
    except (TimeoutError,ValueError,TypeError):
        return ProbeResult(status='UNAVAILABLE',reason_code='server_browser_interrupted')
    finally:
        if process.returncode is None:process.kill()
        await process.wait()
        if not reader.done():reader.cancel()
        await asyncio.gather(reader,return_exceptions=True)


async def tick(pool,renderer=render):
    async with pool.connection() as conn,conn.transaction():
        await scope(conn,worker=True)
        # Global admission lock bounds browser memory even across multiple workers.
        await conn.execute('SELECT pg_advisory_xact_lock(440043)')
        busy=await (await conn.execute("SELECT count(*) AS n FROM retailer_browser_jobs WHERE executor='server' AND state='STARTED' AND expires_at>now()")).fetchone()
        try:limit=max(1,min(2,int(os.environ.get('AI_RETAILER_SERVER_MAX_CONCURRENCY','1'))))
        except ValueError:limit=1
        if busy['n']>=limit:return False
        jobs=await (await conn.execute("SELECT * FROM retailer_browser_jobs WHERE executor='server' AND state='PENDING' AND expires_at>now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 10")).fetchall()
        job=None
        for row in jobs:
            if await job_allowed(conn,row):job=row;break
        if job is None:return False
        token=uuid4()
        await conn.execute("UPDATE retailer_browser_jobs SET state='STARTED',started_at=now(),run_token=%s WHERE id=%s",(token,job['id']))

    async def authorized():
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,worker=True)
            active=await (await conn.execute("SELECT * FROM retailer_browser_jobs WHERE id=%s AND run_token=%s AND state='STARTED' AND expires_at>now()",(job['id'],token))).fetchone()
            return bool(active and await job_allowed(conn,active))
    try:
        probe=await renderer(job,authorized)
        if not await authorized():return True
        probe=ProbeResult.model_validate(probe)
    except asyncio.CancelledError:raise
    except Exception:  # noqa: BLE001 -- never persist remote/browser error strings
        probe=ProbeResult(status='UNAVAILABLE',reason_code='server_browser_interrupted')
    async with pool.connection() as conn,conn.transaction():
        await scope(conn,worker=True)
        # A crash after dispatch never sends the same job to another renderer.
        # The normal task poll records expiry and requires explicit recovery.
        await conn.execute("UPDATE retailer_browser_jobs SET state='DONE',result=%s WHERE id=%s AND run_token=%s AND state='STARTED' AND expires_at>now()",(Jsonb(probe.model_dump()),job['id'],token))
    return True


async def serve(pool):
    while True:
        try:
            if await tick(pool):continue
        except asyncio.CancelledError:raise
        except Exception:  # noqa: BLE001 -- durable queue retained; no secret-bearing logs
            logging.getLogger(__name__).warning('Server retailer tick interrupted; durable state retained')
        await asyncio.sleep(1)

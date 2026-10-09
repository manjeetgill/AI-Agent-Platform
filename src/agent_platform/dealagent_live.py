"""Optional local DealAgent probe bridge; retailer parsers stay in DealAgent."""
import asyncio
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field

from .connector_health import CheckResult, ProbeResult
from .tasks import Action, Strict, TaskError


class LiveInput(Strict):
    retailer: Literal['croma', 'reliancedigital', 'vijaysales', 'jiomart', 'blinkit', 'zepto', 'instamart', 'bigbasket']
    pincode: str | None = Field(default=None, pattern=r'^[1-9][0-9]{5}$')


class DealAgentLiveProbe:
    version, risk, timeout = '1', 'READ', 30
    description = ('Check one configured DealAgent retailer product live using its existing HTTP policies and parser. '
        'No arbitrary URLs, browser interaction, login or collection queue. Reports denied access, '
        'missing rendering/location and unknown fees. Never invent a delivery pincode.')
    input_model, output_model = LiveInput, CheckResult

    def __init__(self, registry):
        self.registry = registry

    def prepare(self, arguments):
        value = LiveInput.model_validate(arguments, strict=True)
        return Action('dealagent.live_probe', self.version, value.model_dump(),
            f'Check the configured {value.retailer} product URL once; no purchase.', 'READ')

    def health_scope(self, action):
        # Location changes never reset the retailer circuit breaker.
        return action.arguments['retailer']

    async def execute(self, action, *, task_id, authorized, **context):
        checker = self.registry.adapters['runtime.connector_check']
        check = checker.prepare({'tool':action.tool, 'version':action.version,
            'arguments':action.arguments, 'require_price':True, 'max_age_seconds':3600})
        return await checker.execute(check, task_id=task_id, authorized=authorized)

    async def health_check(self, action, *, task_id, authorized):
        if not await authorized():
            raise TaskError('Execution authority revoked')
        root = Path(os.environ.get('DEALAGENT_PROBE_ROOT', ''))
        node = Path(os.environ.get('DEALAGENT_PROBE_NODE', ''))
        loader = root/'engine/node_modules/tsx/dist/loader.mjs'
        if not root.is_absolute() or not node.is_absolute() or not node.is_file() or not loader.is_file():
            return ProbeResult(status='CONFIGURATION_REQUIRED', reason_code='probe_runtime_missing').model_dump()
        # Only explicit operator paths are executable. Never inherit model/API
        # credentials, proxy settings or NODE_OPTIONS into the child.
        process = await asyncio.create_subprocess_exec(str(node), '--import', str(loader),
            str(Path(__file__).with_name('probes')/'dealagent.mjs'), str(root),
            action.arguments['retailer'], action.arguments.get('pincode') or '',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={'PATH':str(node.parent)}, limit=32768)
        try:
            async with asyncio.timeout(17):
                async def bounded_output():
                    data = bytearray()
                    while part := await process.stdout.read(4096):
                        data.extend(part)
                        if len(data)>16000:
                            raise TaskError('Probe response too large')
                    return bytes(data)
                read = asyncio.create_task(bounded_output())
                try:
                    while not read.done():
                        await asyncio.wait({read}, timeout=0.2)
                        if not await authorized():
                            raise TaskError('Execution authority revoked')
                    data = await read
                    await process.wait()
                finally:
                    if not read.done():
                        read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                if process.returncode:
                    raise TaskError('Probe runtime failed')
                return ProbeResult.model_validate(json.loads(data), strict=True).model_dump()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def reconcile(self, action, **context):
        return None

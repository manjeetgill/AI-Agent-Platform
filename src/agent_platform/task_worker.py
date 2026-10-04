"""Durable worker; browser execution is only possible through registered site adapters."""
import asyncio
import json
import logging
import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .task_store import TaskStore
from .tasks import Reconciliation

logger = logging.getLogger(__name__)


async def tick(store, registry):
    await store.recover()
    claimed = await store.claim()
    if claimed is None:
        return False
    try:
        adapter, action = registry.resolve(claimed['step'])
        not_applied = False
        async with asyncio.timeout(adapter.timeout):
            if claimed['mode'] == 'RECONCILE':
                verified = await adapter.reconcile(action, task_id=str(claimed['id']))
                if verified is not None and not isinstance(verified, Reconciliation):
                    raise ValueError('Reconciliation requires verified evidence')
                not_applied = verified is not None and not verified.applied
                result = verified.evidence if verified is not None else None
            else:
                if action.risk == 'USER_ONLY':
                    raise RuntimeError('User-only actions cannot execute')

                async def authorized():
                    return await store.authorized(claimed['id'], claimed['lease_token'])

                if not await authorized():
                    return True
                result = await adapter.execute(action, task_id=str(claimed['id']),
                    attempt_id=str(claimed['lease_token']), authorized=authorized)
                if result is not None and hasattr(adapter, 'output_model'):
                    result = adapter.output_model.model_validate(result, strict=True).model_dump()
            # Only bounded, redacted adapter evidence is persisted; never raw browser dumps.
            if result is not None and (not isinstance(result, dict)
                    or len(json.dumps(result, allow_nan=False).encode()) > 16000):
                raise ValueError('Invalid adapter outcome')
        await store.finish(claimed, result, not_applied=not_applied)
    except asyncio.CancelledError:
        await asyncio.shield(store.finish(claimed, failed=True))
        raise
    except Exception:  # noqa: BLE001 -- do not expose page text, tool arguments or credentials in logs
        logger.warning('Task step interrupted task_id=%s attempt_id=%s', claimed['id'], claimed['lease_token'])
        await store.finish(claimed, failed=True)
    return True


async def serve(registry):
    if not registry.adapters:
        raise RuntimeError('No tested site adapters are installed; browser execution remains disabled')
    async with AsyncConnectionPool(os.environ['AI_PLATFORM_DATABASE_URL'], min_size=1, max_size=4,
            kwargs={'row_factory': dict_row, 'connect_timeout': 3,
                    'options': '-c statement_timeout=5000'}, open=False) as pool:
        await pool.wait(timeout=10)
        store = TaskStore(pool)
        while True:
            if not await tick(store, registry):
                await asyncio.sleep(1)


if __name__ == '__main__':
    from .decision_tool import default_registry
    asyncio.run(serve(default_registry()))

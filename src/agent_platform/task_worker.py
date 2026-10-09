"""Durable worker; browser execution is only possible through registered site adapters."""
import asyncio
import json
import logging
import os
import signal

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .db_scope import worker_connection
from .external_jobs import DeferredJob, JobFailure
from .task_store import TaskStore
from .tasks import Reconciliation
from .worker_readiness import reporting

logger = logging.getLogger(__name__)


async def tick(store, registry):
    from .delegation import prune_expired
    await prune_expired(store.pool)
    await store.recover()
    claimed = await store.claim()
    if claimed is None:
        return False
    from .db_scope import model_identity
    marker=model_identity.set({'tenant':claimed['tenant_id'],'app_id':str(claimed.get('source_app_id') or ''),'user':str(claimed['owner_id'])})
    try:
        adapter, action = registry.resolve(claimed['step'])
        not_applied = False
        async with asyncio.timeout(adapter.timeout):
            if claimed['mode'] == 'RECONCILE':
                extra = {}
                if callable(getattr(adapter, 'poll', None)):
                    extra['operation_id'] = f"{claimed['id']}:{claimed['current_step']}:{action.digest}"
                verified = await adapter.reconcile(action, task_id=str(claimed['id']), **extra)
                if isinstance(verified, DeferredJob) and callable(getattr(adapter, 'poll', None)):
                    await store.defer(claimed, verified)
                    return True
                if isinstance(verified, JobFailure):
                    await store.job_failed(claimed, verified)
                    return True
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
                if claimed['mode'] == 'POLL':
                    result = await adapter.poll(action, reference=claimed['step']['external_job_reference'],
                        task_id=str(claimed['id']), authorized=authorized)
                else:
                    extra = {}
                    if callable(getattr(adapter, 'poll', None)):
                        extra['operation_id'] = f"{claimed['id']}:{claimed['current_step']}:{action.digest}"
                    result = await adapter.execute(action, task_id=str(claimed['id']),
                        attempt_id=str(claimed['lease_token']), authorized=authorized, **extra)
                if isinstance(result, DeferredJob):
                    if not callable(getattr(adapter, 'poll', None)):
                        raise TypeError('Deferred tools require a read-only poll method')
                    await store.defer(claimed, result)
                    return True
                if isinstance(result, JobFailure):
                    await store.job_failed(claimed, result)
                    return True
                if claimed['mode'] == 'POLL' and result is None:
                    raise ValueError('Poll returned no verifiable job state')
                if result is not None and hasattr(adapter, 'output_model'):
                    result = adapter.output_model.model_validate(result, strict=True).model_dump()
            # Only bounded, redacted adapter evidence is persisted; never raw browser dumps.
            if result is not None and (not isinstance(result, dict)
                    or len(json.dumps(result, allow_nan=False).encode()) > 16000):
                raise ValueError('Invalid adapter outcome')
        continuation = adapter.continuation(action, result) if result is not None and hasattr(adapter, 'continuation') else None
        await store.finish(claimed, result, not_applied=not_applied, append_actions=continuation)
    except asyncio.CancelledError:
        await asyncio.shield(store.poll_failed(claimed) if claimed['mode'] == 'POLL' else store.finish(claimed, failed=True))
        raise
    except Exception:  # noqa: BLE001 -- do not expose page text, tool arguments or credentials in logs
        logger.warning('Task step interrupted task_id=%s attempt_id=%s', claimed['id'], claimed['lease_token'])
        if claimed['mode'] == 'POLL':
            await store.poll_failed(claimed)
        else:
            await store.finish(claimed, failed=True)
    finally:
        model_identity.reset(marker)
    return True


async def serve(registry):
    if not registry.adapters:
        raise RuntimeError('No tested site adapters are installed; browser execution remains disabled')
    async with AsyncConnectionPool(os.environ.get('AI_PLATFORM_WORKER_DATABASE_URL') or os.environ['AI_PLATFORM_DATABASE_URL'], configure=worker_connection, min_size=1, max_size=4,
            kwargs={'row_factory': dict_row, 'connect_timeout': 3,
                    'options': '-c statement_timeout=5000'}, open=False) as pool:
        from .db_scope import assert_runtime_role
        await assert_runtime_role(pool,'worker')
        await pool.wait(timeout=10)
        from .research_tool import install_research_tool
        install_research_tool(registry, pool)
        from .knowledge import KnowledgeAnswer
        registry.adapters['knowledge.answer'] = KnowledgeAnswer(pool)
        from .flags import enabled
        if enabled('AI_BROWSER_TASKS'):
            from .browser_runner import BrowserSearch
            registry.adapters['browser.wikipedia_search']=BrowserSearch(pool)
        if enabled('AI_AUTOMATIONS'):
            from .adaptive_runtime import install
            install(registry, pool)
        store = TaskStore(pool)
        server_browser=None
        if enabled('AI_AUTOMATIONS') and enabled('AI_RETAILER_BROWSER') and enabled('AI_RETAILER_SERVER_BROWSER'):
            from .server_retailer import serve as serve_server_browser
            server_browser=asyncio.create_task(serve_server_browser(pool))
        try:
            from .server_retailer import runtime_configured
            async with reporting(pool, registry, lambda: bool(
                    server_browser and not server_browser.done() and runtime_configured())):
                while True:
                    from .flags import enabled
                    if enabled('AI_AUTOMATIONS'):
                        from .automation import tick as automation_tick
                        try:
                            await automation_tick(pool, registry)
                        except Exception:  # noqa: BLE001 -- don't log credential-bearing database errors
                            logger.warning('Orchestration tick failed; durable state retained')
                    if enabled('AI_EMAIL_NOTIFICATIONS'):
                        from .email_service import tick as email_tick
                        try:
                            await email_tick(pool)
                        except Exception:  # noqa: BLE001 -- no private notification content in logs
                            logger.warning('Email queue tick failed; durable state retained')
                    if not await tick(store, registry):
                        await asyncio.sleep(1)
        finally:
            if server_browser:
                server_browser.cancel()
                await asyncio.gather(server_browser,return_exceptions=True)



async def main():
    from .decision_tool import default_registry
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, current.cancel)
    try:
        await serve(default_registry())
    except asyncio.CancelledError:
        # Let serve's finally cancel the renderer and reap its child process.
        pass
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

"""Feed tools bind task ownership, encrypted connections and connector check limits."""
import asyncio
from uuid import UUID

from pydantic import Field

from .app_catalogue import policy
from .automation import scope
from .connector_health import CheckInput, CheckResult, ProbeResult, evaluate
from .feed_config import profiles
from .feed_crypto import unseal
from .feed_http import FeedFailure, fetch
from .tasks import Action, Strict, TaskError


class FeedInput(Strict):
    connection_id: str = Field(pattern=r'^[0-9a-f-]{36}$')
    revision: int = Field(ge=1)
    profile_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class FeedRead:
    version, risk, timeout = '1', 'READ', 30
    description = 'Read a configured JSON API/feed using an owned private connection; never invent connection identifiers.'
    input_model, output_model = FeedInput, CheckResult

    def __init__(self, pool, registry, *, transport=None, resolver=None):
        self.pool, self.registry, self.transport, self.resolver = pool, registry, transport, resolver

    def prepare(self, arguments):
        value = FeedInput.model_validate(arguments, strict=True)
        UUID(value.connection_id)
        return Action('feed.read', '1', value.model_dump(), 'Read and verify the configured API/feed.', 'READ')

    def health_scope(self, action):
        return action.arguments['connection_id']

    async def owned(self, action, task_id):
        async with self.pool.connection() as conn, conn.transaction():
            await scope(conn, worker=True)
            row = await (await conn.execute('''SELECT f.* FROM feed_connections f JOIN chore_tasks t
                ON t.source_app_id=f.app_id AND t.owner_id=f.owner_id AND t.tenant_id=f.tenant_id
                WHERE f.id=%s AND t.id=%s''', (action.arguments['connection_id'], task_id))).fetchone()
            if not row or row['disabled'] or row['revision'] != action.arguments['revision']:
                raise TaskError('Connection unavailable or revision changed')
            profile = profiles(row['app_id']).get(row['profile'])
            if not profile or profile.fingerprint != row['profile_hash'] or row['profile_hash'] != action.arguments['profile_hash']:
                raise TaskError('Profile changed; review and reconnect')
            return row, profile

    async def execute(self, action, *, task_id, authorized, **context):
        row, profile = await self.owned(action, task_id)
        checker = self.registry.adapters['runtime.connector_check']
        check = checker.prepare({'tool': 'feed.read', 'version': '1', 'arguments': action.arguments,
            'max_age_seconds': profile.max_age_seconds, 'require_price': profile.require_price})
        result = await checker.execute(check, task_id=task_id, authorized=authorized)
        if result['execution'] == 'DEFERRED':
            if result['needs_attention']:
                async with self.pool.connection() as conn, conn.transaction():
                    await scope(conn, worker=True)
                    last = await (await conn.execute('''SELECT result FROM connector_check_reports
                        WHERE app_id=%s AND owner_id=%s AND tool='feed.read' AND source=%s
                        ORDER BY sequence DESC LIMIT 1''', (row['app_id'], row['owner_id'], str(row['id'])))).fetchone()
                previous = last['result'] if last else {'status':'CONFIGURATION_REQUIRED', 'reason_code':'review_last_report'}
                state = {'LOGIN_REQUIRED':'AUTHORIZATION_REQUIRED', 'CONFIGURATION_REQUIRED':'AUTHORIZATION_REQUIRED'}.get(previous['status'], previous['status'])
                await self.status(row, state, previous['reason_code'])
            else:
                await self.status(row, 'WAITING_RETRY', result['reason_code'])
        return result

    async def status(self, row, state, reason):
        async with self.pool.connection() as conn, conn.transaction():
            await scope(conn, worker=True)
            await conn.execute('''UPDATE feed_connections SET state=%s,reason_code=%s,updated_at=now()
                WHERE id=%s AND revision=%s AND disabled=false''', (state, reason, row['id'], row['revision']))

    async def health_check(self, action, *, task_id, authorized):
        row, profile = await self.owned(action, task_id)

        async def permitted():
            if not await authorized():
                return False
            try:
                await self.owned(action, task_id)
                return True
            except ValueError:
                return False

        async def reserve():
            async with self.pool.connection() as conn, conn.transaction():
                limit = (await policy(conn, row['app_id'])).feed_requests_per_hour
                return bool(await (await conn.execute('''INSERT INTO feed_request_usage(app_id,hour,requests)
                    SELECT %s,date_trunc('hour',now()),1 WHERE %s>0
                    ON CONFLICT(app_id,hour) DO UPDATE SET requests=feed_request_usage.requests+1
                    WHERE feed_request_usage.requests<%s RETURNING requests''', (row['app_id'], limit, limit))).fetchone())
        try:
            if profile.auth != 'none' and not row['credential_cipher']:
                raise FeedFailure('CONFIGURATION_REQUIRED', 'credentials_missing')
            secret = unseal(row['credential_cipher'], row) if profile.auth != 'none' else None
            extra = {'resolver': self.resolver} if self.resolver else {}
            async with asyncio.timeout(18):
                result = await fetch(profile, secret, reserve=reserve, authorized=permitted, transport=self.transport, **extra)
        except FeedFailure as exc:
            result = exc.result
        except TimeoutError:
            result = ProbeResult(status='UNAVAILABLE', reason_code='feed_timeout')
        except Exception:  # noqa: BLE001 -- never expose provider bodies, URLs containing credentials or vault errors
            result = ProbeResult(status='BROKEN', reason_code='invalid_feed_or_configuration')
        inputs = CheckInput(tool='feed.read', version='1', max_age_seconds=profile.max_age_seconds, require_price=profile.require_price)
        _, usable = evaluate(result, inputs)
        state = ('READY' if usable else 'EVIDENCE_REQUIRED') if result.status == 'WORKING' else {
            'LOGIN_REQUIRED': 'AUTHORIZATION_REQUIRED', 'CONFIGURATION_REQUIRED': 'AUTHORIZATION_REQUIRED',
        }.get(result.status, result.status)
        if await permitted():
            await self.status(row, state, result.reason_code)
        return result.model_dump()

    async def reconcile(self, action, **context):
        return None


def install(registry, pool):
    registry.adapters['feed.read'] = FeedRead(pool, registry)

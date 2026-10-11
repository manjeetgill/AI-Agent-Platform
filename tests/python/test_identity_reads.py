"""A returning authenticated user never executes identity INSERT/UPDATE statements."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from uuid import uuid4

from agent_platform.service_identity import authenticate


def test_returning_identity_has_no_writes_and_first_use_handles_race():
    async def check(existing):
        owner, app, key = uuid4(), uuid4(), uuid4()
        secret = 'x'*43
        statements = []

        class Connection:
            def transaction(self):
                return self.context()

            @asynccontextmanager
            async def context(self):
                yield self

            async def execute(self, sql, params):
                statements.append(sql)
                return self

            async def fetchone(self):
                if 'platform_service_keys' in statements[-1]:
                    return {'app_id': app, 'tenant_id': 'fixture', 'scopes': ['tasks:read'],
                            'secret_hash': hashlib.sha256(secret.encode()).hexdigest()}
                reads = sum('SELECT owner_id' in s for s in statements)
                return {'owner_id': owner} if existing or reads > 1 else None

        conn = Connection()

        class Pool:
            def connection(self):
                return conn.context()

        result = await authenticate(Pool(), f'Bearer apk_{key}.{secret}', 'returning-user', 'tasks:read')
        assert result['user'] == str(owner)
        writes = [s for s in statements if s.lstrip().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        assert len(writes) == (0 if existing else 1)
        if not existing:
            assert 'ON CONFLICT DO NOTHING' in writes[0]
    asyncio.run(check(True))
    asyncio.run(check(False))

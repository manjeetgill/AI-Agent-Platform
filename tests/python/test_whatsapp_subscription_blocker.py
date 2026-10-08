import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_platform.whatsapp_opt_in import subscription_blocker


@pytest.mark.parametrize(('row', 'expected'), [
    ({'opted_out': True}, 'Send START'),
    ({'opted_out': False}, 'conversation has expired'),
    (None, 'No message received from this number. Include its country code'),
])
def test_subscription_failure_explains_the_correct_next_action(row, expected):
    cursor = AsyncMock()
    cursor.fetchone.return_value = row
    conn = AsyncMock()
    conn.execute.return_value = cursor
    detail = asyncio.run(subscription_blocker(conn, 'channel', '919876543210', '+917703961610'))
    assert expected in detail
    assert '+917703961610' in detail
    assert '919876543210' not in detail
    conn.execute.assert_awaited_once()

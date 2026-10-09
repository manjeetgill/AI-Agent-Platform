"""CI must execute database tests; local unit-only runs may skip them."""
import os

import pytest


def pytest_sessionstart(session):
    if os.environ.get('CI', '').lower() in {'1', 'true'} and not os.environ.get('AGENT_TEST_DATABASE_URL'):
        raise pytest.UsageError('CI requires a disposable migrated AGENT_TEST_DATABASE_URL; database tests must run.')

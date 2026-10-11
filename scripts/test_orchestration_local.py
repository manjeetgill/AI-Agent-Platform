"""Compatibility entry point; orchestration tests always use restricted roles."""
import asyncio

from test_restricted import main

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

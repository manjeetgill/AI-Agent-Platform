# AI Agent developer guide

The standalone FastAPI app is `agent_platform.app:app`; its schema is in `agent_platform/migrations` and is migrated explicitly by `python -m agent_platform.migrate`. The NRAIAlgo adapter is maintained in the consumer repository; this project accesses it only over HTTP. The read-only runtime is `agent_platform.runtime`, persistence is `agent_platform.repository`, token validation is `agent_platform.tokens`, and strict request/report contracts are `agent_platform.models`.

Built-in definitions are immutable version 1 records created per user from `agent_platform.catalog`. Custom edits insert a new immutable version, while run rows retain the version used. Requests use idempotency keys. The built-ins are Signal Research, Signal Challenger, Strategy Review, Trade Review and Backtest Auditor. Portfolio, momentum, breakout and risk templates remain unavailable until source-specific connectors exist.

The optional workflow engine accepts only `signal_review` and `backtest_review`. Each has exactly two fixed steps, one evidence source, a 115-second overall timeout, a two-minute lease, and a user-scoped idempotency key. A failed step stops the workflow. Step reports remain ordinary versioned agent runs. `AI_WORKFLOWS=1` enables it; there is no arbitrary graph or recursive agent invocation.

Run focused tests with `python -m pytest tests/python/test_agent_platform.py -q` and lint with `ruff check src tests`. Use mock connector/model clients; never call a real broker or place an order in tests.

`agent_platform.evaluation.evaluate` and `tests/fixtures/agent_platform_eval.json` provide deterministic checks for schema validity, source references, and unsupported historical claims. Extend these fixtures when changing prompts, models or evidence contracts. The opt-in `test_agent_platform_postgres.py` uses only an explicitly configured disposable database.

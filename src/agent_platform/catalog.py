"""Immutable built-in definitions; unsupported tools are deliberately absent."""

BUILTINS = {
    'signal-research': {
        'name': 'Signal Research', 'description': 'Explain a scanner signal from its saved evidence.',
        'instructions': 'Explain the trigger, supporting and conflicting evidence, freshness and missing context.',
        'allowed_tools': ['scanner_evidence'], 'version': 1,
    },
    'signal-challenger': {
        'name': 'Signal Challenger', 'description': 'Challenge scanner evidence and identify limitations.',
        'instructions': 'Focus on conflicting indicators, stale data, weak volume, and missing context.',
        'allowed_tools': ['scanner_evidence'], 'version': 1,
    },
    'strategy-review': {
        'name': 'Strategy Review', 'description': 'Review one saved archive backtest and its measured limitations.',
        'instructions': 'Explain observed outcome, P&L, drawdown, costs, coverage and missing robustness evidence. This is one basket, not a statistical strategy sample.',
        'allowed_tools': ['backtest_evidence'], 'version': 1,
    },
    'trade-review': {
        'name': 'Trade Review', 'description': 'Review one completed simulated options paper trade.',
        'instructions': 'Explain the recorded simulated entry and exit, deterministic P&L and fees. Distinguish paper fills from broker execution.',
        'allowed_tools': ['paper_trade_evidence'], 'version': 1,
    },
    'backtest-auditor': {
        'name': 'Backtest Auditor', 'description': 'Find coverage, fill and cost limitations in one saved archive backtest.',
        'instructions': 'Audit missing data, sample size, fills, slippage, costs, parameter and regime uncertainty. Never claim statistical validity from completion.',
        'allowed_tools': ['backtest_evidence'], 'version': 1,
    },
}

TEMPLATES = [
    {'name': name, 'description': description,
     'available': name in ('Signal Analyst', 'Signal Challenger', 'Strategy Reviewer', 'Trade Reviewer', 'Backtest Auditor')}
    for name, description in (
        ('Signal Analyst', 'Explain scanner evidence'), ('Signal Challenger', 'Challenge scanner evidence'),
        ('Strategy Reviewer', 'Review a backtest'), ('Trade Reviewer', 'Review completed trades'),
        ('Backtest Auditor', 'Audit backtest limitations'), ('Portfolio Analyst', 'Review a portfolio'),
        ('Momentum Scout', 'Find momentum setups'), ('Breakout Researcher', 'Review breakouts'),
        ('Risk Monitor', 'Monitor risk conditions'))
]

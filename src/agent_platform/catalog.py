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

# Domain computations are supplied by the consuming app's scoped snapshot connector.
for slug, name, scope in (
    ('historical-patterns', 'Historical Pattern Finder', 'Explain measured historical similarity, overlap exclusions, outcomes and sample-size limitations. Do not infer predictive probabilities.'),
    ('risk-monitor-review', 'Risk Monitor', 'Explain measured concentration and coverage. Monitoring schedules and notification consent are configured explicitly in the consumer UI; never claim a monitor was created by this report.'),
    ('portfolio-risk', 'Portfolio Risk Analyst', 'Explain valued holdings concentration, mapping coverage, missing accounts and freshness. Never estimate beta or correlation.'),
    ('market-context', 'Market Context Analyst', 'Explain dated sector/index mapping, selected-universe breadth and verified volatility. Distinguish selected breadth from exchange breadth.'),
    ('trade-autopsy', 'Multi-trade Autopsy', 'Explain deterministic completed trade metrics and sample limitations. Paper fills are simulated.'),
    ('trading-journal', 'Trading Journal Agent', 'Summarize dated trading metrics, observed entry-hour patterns and reflection questions.'),
    ('trader-behavior', 'Trader Behavior Agent', 'Explain measured holding-time and post-loss premium-size comparisons. Never infer motives, emotions or causality.'),
    ('strategy-doctor', 'Strategy Doctor', 'Diagnose saved backtest coverage and cost dependence. Treat each basket as one observation; distinguish hypotheses from measured evidence.'),
    ('strategy-comparator', 'Strategy Comparator', 'Compare only explicitly supplied periods, capital and cost conventions. Do not rank incompatible samples or invent Sharpe or CAGR.'),
    ('options-analyst', 'Options Analyst', 'Explain verified option contracts, calibrated Greeks, per-underlying exposure and expiry payoff limitations. Do not propose orders.'),
    ('robustness-review', 'Strategy Robustness Review', 'Explain approved fixed-fill cost sensitivity results when supplied; otherwise state sensitivity is missing. Broader searches require a separately approved bounded plan.'),
):
    BUILTINS[slug] = {'name': name, 'description': scope, 'instructions': scope,
                      'allowed_tools': ['research_evidence'], 'version': 1}

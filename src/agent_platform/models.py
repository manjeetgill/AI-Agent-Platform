"""Strict schemas for the first read-only agent capability."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ScannerInput(Strict):
    tool: Literal['scanner_evidence'] = 'scanner_evidence'
    run_id: str = Field(pattern=r'^[0-9a-fA-F-]{36}$')
    instrument: str = Field(pattern=r'^NSE:[A-Z0-9&_.-]{1,40}$')


class BacktestInput(Strict):
    tool: Literal['backtest_evidence']
    backtest_id: str = Field(pattern=r'^[0-9a-fA-F-]{36}$')


class PaperTradeInput(Strict):
    tool: Literal['paper_trade_evidence']
    trade_id: str = Field(pattern=r'^[0-9a-fA-F-]{36}$')


class AgentDraft(Strict):
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(default='', max_length=500)
    instructions: str = Field(min_length=1, max_length=4000)
    allowed_tools: list[Literal['scanner_evidence', 'backtest_evidence', 'paper_trade_evidence']] = Field(
        default_factory=list, max_length=3)


class AgentIdea(Strict):
    request: str = Field(min_length=10, max_length=1500)


class AgentSuggestion(Strict):
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(max_length=500)
    instructions: str = Field(min_length=1, max_length=4000)


class PreferenceIn(Strict):
    content: str = Field(min_length=1, max_length=500)


class StrategyLeg(Strict):
    action: Literal['buy', 'sell']
    right: Literal['CE', 'PE']
    offset: int = Field(ge=-10, le=10)
    lots: Literal[1] = 1
    strike: float | None = Field(default=None, gt=0, le=1000000)
    units: int = Field(ge=1, le=100000)


class StrategyDraft(Strict):
    name: str = Field(min_length=1, max_length=80)
    source: Literal['archive'] = 'archive'
    underlying: Literal['NIFTY', 'BANKNIFTY']
    expiry: date
    start: date
    end: date
    entryRule: Literal['first-close', 'above-sma5', 'below-sma5']
    legs: list[StrategyLeg] = Field(min_length=1, max_length=4)
    capital: float = Field(ge=1000, le=100000000)
    stopLoss: float | None = Field(default=None, gt=0, le=10000000)
    profitTarget: float | None = Field(default=None, gt=0, le=10000000)
    dailyLossLimit: float | None = Field(default=None, gt=0, le=10000000)
    cadence: Literal['once'] = 'once'
    feePerFill: float = Field(ge=0, le=1000)
    levyBps: float = Field(ge=0, le=100)
    slippageTicks: Literal[0] = 0
    archiveSlippageBps: float = Field(ge=0, le=500)

    @model_validator(mode='after')
    def supported_window(self):
        if not self.start < self.end < self.expiry or (self.end - self.start).days > 370:
            raise ValueError('Invalid archive backtest window')
        if any(leg.strike is not None and leg.offset != 0 for leg in self.legs):
            raise ValueError('Explicit strikes require offset zero')
        return self


class StrategyPlan(Strict):
    supported: bool
    interpretation: str = Field(max_length=1000)
    draft: StrategyDraft | None
    missing: list[str] = Field(default_factory=list, max_length=15)

    @model_validator(mode='after')
    def supported_has_draft(self):
        if self.supported != (self.draft is not None):
            raise ValueError('Support state and draft disagree')
        return self


class RunRequest(Strict):
    agent_id: str = Field(pattern=r'^[0-9a-fA-F-]{36}$')
    evidence: ScannerInput | BacktestInput | PaperTradeInput
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')


class WorkflowRequest(Strict):
    kind: Literal['signal_review', 'backtest_review']
    evidence: ScannerInput | BacktestInput
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=r'^[A-Za-z0-9_.:-]+$')


class Finding(Strict):
    category: Literal['FACT', 'CALCULATED FACT', 'OBSERVATION', 'INTERPRETATION', 'HYPOTHESIS']
    text: str = Field(min_length=1, max_length=600)
    source_ids: list[str] = Field(default_factory=list, max_length=8)


class Report(Strict):
    summary: str = Field(min_length=1, max_length=1500)
    why_triggered: str | None = Field(default=None, max_length=1000)
    supporting: list[Finding] = Field(default_factory=list, max_length=12)
    conflicting: list[Finding] = Field(default_factory=list, max_length=12)
    missing: list[str] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=12)
    historical_context: list[str] = Field(default_factory=list, max_length=8)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(default_factory=list, max_length=8)
    data_freshness: str = Field(max_length=300)

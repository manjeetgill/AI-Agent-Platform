"""Typed action contracts. Only operator-installed adapters can execute tools."""
import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class TaskError(ValueError):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TaskCreate(Strict):
    request: str = Field(min_length=5, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_.:-]+$')


class StepInput(Strict):
    tool: str = Field(min_length=1, max_length=100)
    arguments: dict


class PlanInput(Strict):
    steps: list[StepInput] = Field(min_length=1, max_length=30)


class ApprovalInput(Strict):
    action_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['yes']


@dataclass(frozen=True)
class Action:
    tool: str
    version: str
    arguments: dict
    summary: str
    risk: Literal['READ', 'SEND', 'USER_ONLY']

    @property
    def digest(self):
        encoded = json.dumps({'tool': self.tool, 'version': self.version,
            'arguments': self.arguments, 'summary': self.summary, 'risk': self.risk},
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Reconciliation:
    applied: bool
    evidence: dict

    def __post_init__(self):
        if type(self.applied) is not bool or not isinstance(self.evidence, dict) or not self.evidence:
            raise TaskError('Reconciliation requires a definite outcome and evidence')


class Adapter(Protocol):
    version: str
    risk: Literal['READ', 'SEND', 'USER_ONLY']
    timeout: float

    def prepare(self, arguments: dict) -> Action:
        """Validate typed inputs and produce a factual summary. Never call the model here."""
        ...

    async def execute(self, action: Action, *, task_id: str, attempt_id: str, authorized):
        """Check authorized() immediately before acting; honor cancellation. Return redacted evidence."""
        ...

    async def reconcile(self, action: Action, *, task_id: str):
        """Read real state only. Return Reconciliation, or None when uncertain."""
        ...


class Registry:
    def __init__(self, adapters=None):
        self.adapters = dict(adapters or {})

    def prepare(self, step: StepInput):
        adapter = self.adapters.get(step.tool)
        if adapter is None:
            raise TaskError('Unsupported tool; a tested site adapter is required')
        action = adapter.prepare(step.arguments)
        if (action.tool != step.tool or action.version != adapter.version or action.risk != adapter.risk
                or action.risk not in {'READ', 'SEND', 'USER_ONLY'} or not action.summary
                or len(action.summary) > 2000 or not 0 < adapter.timeout <= 30):
            raise TaskError('Invalid adapter contract')
        if len(json.dumps(action.arguments, allow_nan=False).encode()) > 16000:
            raise TaskError('Action is too large')
        return action

    def resolve(self, row):
        action = self.prepare(StepInput(tool=row['tool'], arguments=row['arguments']))
        if action.digest != row['action_hash']:
            raise TaskError('Adapter or action changed; create and approve a new plan')
        return self.adapters[action.tool], action

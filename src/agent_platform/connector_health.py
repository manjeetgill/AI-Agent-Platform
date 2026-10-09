"""Provider-neutral health evidence. Adapters supply observations; no LLM invents them."""
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .tasks import Strict

Health = Literal['WORKING', 'BLOCKED', 'RATE_LIMITED', 'LOGIN_REQUIRED',
                 'CONFIGURATION_REQUIRED', 'BROKEN', 'UNAVAILABLE']


class Observation(Strict):
    source_url: str = Field(max_length=1000)
    observation_id: str = Field(min_length=1, max_length=160)
    observed_at: str = Field(max_length=40)
    # Generic evidence fields; null means not observed, never zero/free.
    price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    fees_known: bool = False
    title: str | None = Field(default=None, max_length=500)
    variant: str | None = Field(default=None, max_length=160)
    location_label: str | None = Field(default=None, max_length=160)
    location_required: bool = False
    location_confirmed: bool = False
    quality_issues: list[str] = Field(default_factory=list, max_length=10)

    @field_validator('quality_issues')
    @classmethod
    def issue_codes(cls, values):
        import re
        if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', value) for value in values):
            raise ValueError('Quality issues must be bounded codes')
        return values

    @field_validator('source_url')
    @classmethod
    def public_reference(cls, value):
        parsed = urlsplit(value)
        # Evidence links are references, never fetched by this module. Strip private
        # query parameters in the adapter before returning a public canonical URL.
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use a public canonical HTTPS URL without credentials, query or fragment')
        return value

    @field_validator('observed_at')
    @classmethod
    def timestamp(cls, value):
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError('Observation requires a timezone')
        return value


class ProbeDetails(Strict):
    source_url: str = Field(max_length=1000)
    checked_at: str = Field(max_length=40)
    parser_version: str = Field(max_length=80)
    http_status: int | None = Field(default=None, ge=100, le=599)
    robots_http_status: int | None = Field(default=None, ge=100, le=599)
    http_requests: int = Field(ge=0, le=12)
    product_requests: int = Field(ge=0, le=6)
    location_confirmed: bool = False
    transport: Literal['http'] = 'http'

    _url = field_validator('source_url')(Observation.public_reference.__func__)
    _time = field_validator('checked_at')(Observation.timestamp.__func__)


class BrowserProbeDetails(Strict):
    """Counts only: no page contents, request bodies, query strings or headers."""
    transport: Literal['server_browser'] = 'server_browser'
    http_status: int | None = Field(default=None, ge=100, le=599)
    requests: int = Field(ge=0, le=10000)
    blocked_resources: int = Field(ge=0, le=10000)
    failed_requests: int = Field(ge=0, le=10000)
    request_limit_reached: bool = False


class ProbeResult(Strict):
    status: Health
    # Codes rather than raw provider messages, which can contain secrets/page text.
    reason_code: str = Field(pattern=r'^[a-z][a-z0-9_]{0,79}$')
    observations: list[Observation] = Field(default_factory=list, max_length=10)
    retry_after_seconds: int | None = Field(default=None, ge=60, le=86400)
    # Unspecified probes must never be advertised as fresh retailer checks.
    verification_mode: Literal['unspecified', 'stored_observation', 'live_source'] = 'unspecified'
    probe: ProbeDetails | BrowserProbeDetails | None = None


class RecoveryOption(Strict):
    action: Literal['authorize', 'configure', 'contact_provider', 'setup_feed', 'wait', 'review_evidence', 'repair_adapter']
    summary: str = Field(max_length=500)
    profile: str | None = None
    setup_path: str | None = None


class CheckInput(Strict):
    tool: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=40)
    arguments: dict = Field(default_factory=dict)
    max_age_seconds: int = Field(default=3600, ge=60, le=604800)
    require_observations: bool = True
    require_price: bool = False


class CheckResult(Strict):
    connector: str
    source: str = 'default'
    execution: Literal['EXECUTED', 'CACHED', 'DEFERRED']
    status: Literal['WORKING', 'BLOCKED', 'RATE_LIMITED', 'LOGIN_REQUIRED',
                    'CONFIGURATION_REQUIRED', 'BROKEN', 'UNAVAILABLE', 'NOT_CHECKED']
    reason_code: str
    report_id: str | None = None
    checked_at: str | None = None
    next_check_at: str | None = None
    needs_attention: bool = False
    usable_evidence: bool = False
    evidence_issues: list[str] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    verification_mode: Literal['unspecified', 'stored_observation', 'live_source'] = 'unspecified'
    recovery_options: list[RecoveryOption] = Field(default_factory=list, max_length=6)
    probe: ProbeDetails | BrowserProbeDetails | None = None


def evaluate(result, inputs, *, now=None):
    """Schema validity/freshness is not a guarantee that a retailer's claim is true."""
    now = now or datetime.now(UTC)
    evidence = []
    for observation in result.observations:
        age = (now-datetime.fromisoformat(observation.observed_at)).total_seconds()
        issues = list(observation.quality_issues)
        if observation.location_required and not (observation.location_confirmed and observation.location_label):
            issues.append('location_unconfirmed')
        if age < 0:
            issues.append('future_timestamp')
        elif age > inputs.max_age_seconds:
            issues.append('stale')
        if inputs.require_price and (observation.price is None or observation.currency is None):
            issues.append('missing_price_or_currency')
        evidence.append({**observation.model_dump(), 'issues': issues,
                         'unknown_fees': not observation.fees_known, 'usable': not issues})
    usable = result.status == 'WORKING' and (bool(evidence) or not inputs.require_observations) and all(e['usable'] for e in evidence)
    return evidence, usable

"""Per-application SMS routes. Credentials remain in server environment variables."""
import hashlib
import ipaddress
import json
import os
import re
from string import Formatter
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .email_config import Limits
from .tasks import Strict


class Template(Strict):
    text: str = Field(min_length=1, max_length=1600)
    template_id: str | None = Field(default=None, pattern=r'^[0-9]{1,30}$')
    approved: bool = False

    @property
    def variables(self):
        fields = set()
        for _, name, spec, conversion in Formatter().parse(self.text):
            if name is not None:
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', name) or spec or conversion:
                    raise ValueError('Only simple named template variables are supported')
                fields.add(name)
        return fields

    @model_validator(mode='after')
    def validate_template(self):
        _ = self.variables  # Validate named placeholders before accepting configuration.
        return self


class Config(Strict):
    provider: Literal['mock', 'smsindiahub', 'traccar_local'] = 'mock'
    key_env: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{1,100}$')
    gateway_url: str | None = None
    allow_lan_http: bool = False
    sim_slot: int = Field(default=1, ge=1, le=3)
    test_recipients: list[str] = Field(default_factory=list, max_length=10)
    sender_id: str | None = Field(default=None, pattern=r'^[A-Za-z]{6}$')
    pe_id: str | None = Field(default=None, pattern=r'^[0-9]{1,30}$')
    templates: dict[str, Template] = Field(min_length=1, max_length=30)
    limits: Limits = Field(default_factory=lambda: Limits(daily=10, monthly=100, per_minute=5))
    testing_no_volume_caps: bool = False
    max_segments: int = Field(default=1, ge=1, le=10)

    @model_validator(mode='after')
    def validate_route(self):
        if self.testing_no_volume_caps and self.provider != 'traccar_local':
            raise ValueError('Uncapped testing is available only for an allowlisted Traccar pilot')
        if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,59}', k) for k in self.templates):
            raise ValueError('Invalid template name')
        if self.provider == 'smsindiahub' and (not self.key_env or not self.sender_id or not self.pe_id
                or any(not t.approved or not t.template_id for t in self.templates.values())):
            raise ValueError('Live SMS requires an authorised sender, PE ID and approved DLT templates')
        if self.provider == 'traccar_local':
            validate_gateway_url(self.gateway_url, self.allow_lan_http)
            if not self.key_env or not self.test_recipients or self.max_segments != 1:
                raise ValueError('Android pilot requires credentials, test recipients and single-segment messages')
            if any(not re.fullmatch(r'91[6-9][0-9]{9}', n) for n in self.test_recipients):
                raise ValueError('Test recipients must use 91 country code')
        return self

    @property
    def fingerprint(self):
        excluded = {'limits', 'testing_no_volume_caps'}
        if self.provider != 'traccar_local':
            # Preserve fingerprints for existing queued mock/provider messages.
            excluded |= {'gateway_url','allow_lan_http','sim_slot','test_recipients'}
        return hashlib.sha256(json.dumps(self.model_dump(exclude=excluded), sort_keys=True).encode()).hexdigest()


def effective_limits(settings, limits=None):
    result = (limits or settings.limits).model_dump()
    if settings.testing_no_volume_caps:
        result.update(daily=None, monthly=None)
    return result


def config(app_id):
    item = json.loads(os.environ.get('AI_SMS_APPS', '{}')).get(str(app_id))
    if not item:
        raise ValueError('SMS is not configured for this application')
    return Config.model_validate(item)


def api_key(settings):
    key = os.environ.get(settings.key_env or '', '')
    if settings.provider == 'smsindiahub' and (not key.strip() or any(c in key for c in '\r\n')):
        raise ValueError('SMS provider credential is missing')
    if settings.provider == 'traccar_local':
        traccar_key(settings)
    return key


def global_limits():
    return Limits.model_validate_json(os.environ.get('AI_SMS_GLOBAL_LIMITS', '{"daily":10,"monthly":100,"per_minute":5}'))


class SMSRequest(Strict):
    idempotency_key: UUID
    to: str
    template: str = Field(pattern=r'^[a-z][a-z0-9_]{0,59}$')
    variables: dict[str, str] = Field(default_factory=dict, max_length=20)
    category: Literal['transactional'] = 'transactional'
    expires_in_seconds: int = Field(default=300, ge=30, le=86400)

    @field_validator('to')
    @classmethod
    def number(cls, value):
        # Only this adapter's tested Indian mobile route. No implicit country guessing.
        if not re.fullmatch(r'\+?91[6-9][0-9]{9}', value):
            raise ValueError('Use an Indian mobile number with +91 country code')
        return value.lstrip('+')


# GSM-7 basic/extension characters; extension characters take two septets.
GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
EXT = set('^{}\\[~]|€\f')


def encoding(text):
    if all(c in GSM or c in EXT for c in text):
        units = sum(2 if c in EXT else 1 for c in text)
        return '0', 1 if units <= 160 else (units + 152) // 153
    units = len(text.encode('utf-16-be')) // 2
    return '8', 1 if units <= 70 else (units + 66) // 67


def render(body, settings):
    if settings.provider == 'traccar_local' and body.to not in settings.test_recipients:
        raise ValueError('Recipient is not enabled for this Android pilot')
    template = settings.templates.get(body.template)
    if not template or set(body.variables) != template.variables:
        raise ValueError('Template variables do not match the configured template')
    if any(not v or len(v) > 512 or any(ord(c)<32 for c in v) for v in body.variables.values()):
        raise ValueError('Invalid template value')
    message = template.text.format_map(body.variables)
    if len(message) > 1600:
        raise ValueError('SMS is too long')
    dcs, segments = encoding(message)
    if segments > settings.max_segments:
        raise ValueError('SMS exceeds configured segment limit')
    return {'to':body.to, 'message':message, 'dcs':dcs, 'segments':segments,
            'template_id':template.template_id, 'sender_id':settings.sender_id, 'pe_id':settings.pe_id}


def validate_gateway_url(value, allow_http=False):
    """Operator-configured LAN IPv4 only: no DNS, redirects, metadata or public hosts."""
    try:
        url = urlsplit(value or '')
        ip = ipaddress.IPv4Address(url.hostname or '')
        networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
        if not any(ip in ipaddress.ip_network(n) for n in networks):
            raise ValueError()
        if url.scheme not in {'http', 'https'} or (url.scheme == 'http' and not allow_http):
            raise ValueError()
        if url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}:
            raise ValueError()
        if not url.port:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Use a fixed LAN IPv4 gateway URL and explicitly acknowledge HTTP for a trusted LAN') from None
    return value.rstrip('/')


def traccar_key(settings):
    key = os.environ.get(settings.key_env or '', '')
    if not key or len(key) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Private Traccar local API key is missing or invalid')
    return key

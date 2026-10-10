"""Per-application SMS routes. Credentials remain in server environment variables."""
import hashlib
import json
import os
import re
from string import Formatter
from typing import Literal
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
    provider: Literal['mock', 'smsindiahub'] = 'mock'
    key_env: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{1,100}$')
    sender_id: str | None = Field(default=None, pattern=r'^[A-Za-z]{6}$')
    pe_id: str | None = Field(default=None, pattern=r'^[0-9]{1,30}$')
    templates: dict[str, Template] = Field(min_length=1, max_length=30)
    limits: Limits = Field(default_factory=lambda: Limits(daily=10, monthly=100, per_minute=5))
    max_segments: int = Field(default=1, ge=1, le=10)

    @model_validator(mode='after')
    def validate_route(self):
        if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,59}', k) for k in self.templates):
            raise ValueError('Invalid template name')
        if self.provider == 'smsindiahub' and (not self.key_env or not self.sender_id or not self.pe_id
                or any(not t.approved or not t.template_id for t in self.templates.values())):
            raise ValueError('Live SMS requires an authorised sender, PE ID and approved DLT templates')
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(exclude={'limits'}), sort_keys=True).encode()).hexdigest()


def config(app_id):
    item = json.loads(os.environ.get('AI_SMS_APPS', '{}')).get(str(app_id))
    if not item:
        raise ValueError('SMS is not configured for this application')
    return Config.model_validate(item)


def api_key(settings):
    key = os.environ.get(settings.key_env or '', '')
    if settings.provider == 'smsindiahub' and (not key.strip() or any(c in key for c in '\r\n')):
        raise ValueError('SMS provider credential is missing')
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

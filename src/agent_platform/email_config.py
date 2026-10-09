"""Explicit operator-only routing. No automatic provider or development fallback."""
import base64
import hashlib
import hmac
import json
import os
import re
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import Field, model_validator

from .tasks import Strict


class Limits(Strict):
    daily: int = Field(default=100, ge=0, le=100000)
    monthly: int = Field(default=1000, ge=0, le=1000000)
    per_minute: int = Field(default=30, ge=1, le=10000)


class SMTPConfig(Strict):
    host: str = Field(min_length=1, max_length=253, pattern=r'^[A-Za-z0-9.:-]+$')
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal['starttls', 'tls', 'loopback'] = 'starttls'
    username: str | None = Field(default=None, max_length=254)

    @model_validator(mode='after')
    def validate_transport(self):
        # Plain SMTP is exclusively for a same-machine relay or development sink.
        if self.security == 'loopback' and (self.host not in {'127.0.0.1', '::1'} or self.username):
            raise ValueError('Plain SMTP requires a literal loopback address without authentication')
        if self.username is not None and (not self.username or any(c in self.username for c in '\r\n')):
            raise ValueError('Invalid SMTP username')
        return self


class Config(Strict):
    provider: Literal['mock','resend','brevo','smtp']
    from_email: str
    from_name: str = Field(default='Notifications', min_length=1, max_length=100)
    key_env: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{1,100}$')
    allowed_link_hosts: list[str] = Field(default_factory=list, max_length=30)
    limits: Limits = Field(default_factory=Limits)
    smtp: SMTPConfig | None = None

    @model_validator(mode='after')
    def validate_config(self):
        email_address(self.from_email)
        if any(c in self.from_name for c in '\r\n<>') or (self.provider in {'resend','brevo'} and not self.key_env):
            raise ValueError('Sender or credential reference is invalid')
        if self.provider == 'smtp':
            if not self.smtp or (self.smtp.username and not self.key_env):
                raise ValueError('SMTP configuration or password reference is missing')
        elif self.smtp:
            raise ValueError('SMTP settings require the SMTP provider')
        if any(not re.fullmatch(r'[a-z0-9.-]{1,253}', h) for h in self.allowed_link_hosts):
            raise ValueError('Invalid allowed link host')
        return self

    @property
    def fingerprint(self):
        route = self.model_dump(exclude={'limits'})
        if self.smtp is None:
            route.pop('smtp')  # Preserve existing queued route fingerprints.
        return hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()


def email_address(value):
    if not isinstance(value, str) or len(value)>254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise ValueError('Invalid email address')
    return value


def config(app_id):
    settings = json.loads(os.environ.get('AI_EMAIL_APPS', '{}')).get(str(app_id))
    if not settings:
        raise ValueError('Email is not configured for this application')
    return Config.model_validate(settings)


def global_limits():
    return Limits.model_validate_json(os.environ.get('AI_EMAIL_GLOBAL_LIMITS', '{}'))


def api_key(settings):
    key = os.environ.get(settings.key_env or '', '')
    required = settings.provider in {'resend', 'brevo'} or (settings.provider == 'smtp' and settings.smtp.username)
    if required and (not key or '\n' in key or '\r' in key):
        raise ValueError('Provider credential is not configured')
    return key


def keys():
    current = os.environ.get('AI_NOTIFICATION_ENCRYPTION_KEY_ID', '')
    ring = {k:base64.b64decode(v, validate=True) for k,v in json.loads(os.environ.get('AI_NOTIFICATION_ENCRYPTION_KEYS','{}')).items()}
    if current not in ring or any(len(v)!=32 for v in ring.values()):
        raise ValueError('Notification encryption is not configured')
    return current, ring


def digest(value):
    key = base64.b64decode(os.environ.get('AI_NOTIFICATION_HASH_KEY',''), validate=True)
    if len(key)!=32:
        raise ValueError('Notification hashing is not configured')
    return hmac.new(key, json.dumps(value,sort_keys=True,separators=(',',':')).encode(), hashlib.sha256).hexdigest()


def aad(row):
    return ':'.join(str(row[k]) for k in ('tenant_id','app_id','owner_id','id')).encode()


def seal(value, row):
    current, ring = keys()
    nonce = os.urandom(12)
    cipher = AESGCM(ring[current]).encrypt(nonce,json.dumps(value).encode(),aad(row))
    return json.dumps({'key':current,'data':base64.b64encode(nonce+cipher).decode()})


def unseal(value, row):
    _, ring = keys()
    item = json.loads(value)
    cipher = base64.b64decode(item['data'], validate=True)
    return json.loads(AESGCM(ring[item['key']]).decrypt(cipher[:12],cipher[12:],aad(row)))

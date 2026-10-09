"""Operator-owned JSON API/feed profiles. Callers cannot choose network destinations."""
import hashlib
import ipaddress
import json
import os
import re
import socket
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .tasks import Strict


class SourceBinding(Strict):
    tool: str = Field(min_length=1, max_length=100)
    source: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$')


class Profile(Strict):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=1000)
    auth: Literal['none', 'bearer', 'header'] = 'bearer'
    auth_header: str = Field(default='X-API-Key', pattern=r'^[A-Za-z][A-Za-z0-9-]{0,59}$')
    items_pointer: str = '/items'
    fields: dict[str, str]
    pagination: Literal['none', 'page', 'cursor'] = 'none'
    page_parameter: str = Field(default='page', pattern=r'^[a-zA-Z][a-zA-Z0-9_]{0,39}$')
    next_pointer: str = '/next_cursor'
    max_pages: int = Field(default=1, ge=1, le=5)
    max_items: int = Field(default=10, ge=1, le=10)
    max_age_seconds: int = Field(default=3600, ge=60, le=604800)
    require_price: bool = False
    setup_help: str = Field(default='Obtain approved API access from this provider, then enter its key privately.', max_length=500)
    alternatives: list[str] = Field(default_factory=list, max_length=5)
    replaces: list[SourceBinding] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def valid(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in {None, 443}):
            raise ValueError('A fixed HTTPS endpoint on port 443 is required')
        if self.auth_header.lower() in {'host', 'cookie', 'connection', 'content-length', 'transfer-encoding', 'proxy-authorization'}:
            raise ValueError('Invalid credential header')
        required = {'source_url', 'observation_id', 'observed_at'}
        allowed = required | {'price', 'currency', 'fees_known'}
        if not required <= self.fields.keys() or not self.fields.keys() <= allowed:
            raise ValueError('Provide an explicit observation mapping')
        for pointer in [self.items_pointer, self.next_pointer, *self.fields.values()]:
            if len(pointer)>300 or len(pointer.split('/'))>11 or not re.fullmatch(r'(?:/(?:[^~]|~[01])*)?', pointer):
                raise ValueError('Invalid JSON pointer')
        if any(len(value)>300 for value in self.alternatives):
            raise ValueError('Alternative description too long')
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


def profiles(app_id):
    raw = json.loads(os.environ.get('AI_FEED_PROFILES', '{}')).get(str(app_id), {})
    if not isinstance(raw, dict) or len(raw)>50:
        raise ValueError('Invalid profile catalogue')
    if any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,59}', slug) for slug in raw):
        raise ValueError('Invalid profile identifier')
    return {slug: Profile.model_validate(value) for slug, value in raw.items()}


def public_address(host):
    """Resolve once, reject private/mixed DNS answers, and pin the request to one IP."""
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    ips = [ipaddress.ip_address(address) for address in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ValueError('Provider destination must resolve only to public addresses')
    return addresses[0]

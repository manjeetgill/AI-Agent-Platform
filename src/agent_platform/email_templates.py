"""Version-one, escaped transactional templates; apps own token generation and validation."""
import html
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from .email_config import email_address
from .tasks import Strict

TEMPLATES = {
    'email_verification': {'fields':['url'], 'max_ttl':86400, 'subject':'Verify your email'},
    'password_reset': {'fields':['url'], 'max_ttl':3600, 'subject':'Reset your password'},
    'otp': {'fields':['code'], 'max_ttl':900, 'subject':'Your verification code'},
    'price_drop': {'fields':['product','price','url'], 'max_ttl':86400, 'subject':'A watched price has dropped'},
    'system_notification': {'fields':['message'], 'max_ttl':86400, 'subject':'Account notification'},
}


class EmailRequest(Strict):
    idempotency_key: UUID
    channel: Literal['email'] = 'email'
    category: Literal['transactional'] = 'transactional'
    to: str = Field(max_length=254)
    template: Literal['email_verification','password_reset','otp','price_drop','system_notification']
    template_version: Literal[1] = 1
    variables: dict[str,str] = Field(max_length=5)
    expires_in_seconds: int = Field(default=600, ge=60, le=86400)


def render(body, settings):
    email_address(body.to)
    spec = TEMPLATES[body.template]
    if set(body.variables)!=set(spec['fields']) or body.expires_in_seconds>spec['max_ttl']:
        raise ValueError('Template fields or expiry are invalid')
    for value in body.variables.values():
        if not value or len(value)>2000 or any(ord(c)<32 and c not in '\n\t' for c in value):
            raise ValueError('Invalid template value')
    v = body.variables
    if 'url' in v:
        url = urlsplit(v['url'])
        if url.scheme!='https' or url.username or url.password or url.hostname not in settings.allowed_link_hosts or url.port not in (None,443):
            raise ValueError('Template link must use an operator-approved HTTPS host')
    if body.template=='otp':
        if not re.fullmatch(r'[A-Za-z0-9]{4,12}',v['code']):
            raise ValueError('Invalid verification code')
        text = f"Your verification code is {v['code']}. Use it to complete your requested verification. If it has expired, request a new code. Do not share this code."
    elif body.template in ('email_verification','password_reset'):
        action = 'verify your email' if body.template=='email_verification' else 'reset your password'
        text = f"Use this link to {action}:\n{v['url']}\nIf the link has expired, request a new one. If you did not request this, ignore this email."
    elif body.template=='price_drop':
        text = f"The price of your watched item {v['product']} is now {v['price']}.\nCheck current availability and fees: {v['url']}"
    else:
        text = v['message']
    text = settings.from_name+'\n\n'+text
    return {'to':body.to,'from_email':settings.from_email,'from_name':settings.from_name,
            'subject':spec['subject'],'text':text,'html':'<p>'+html.escape(text).replace('\n','<br>')+'</p>'}

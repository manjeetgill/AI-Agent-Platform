"""Short-lived, audience-restricted HMAC delegations shared with connector hosts."""
import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4


class TokenError(ValueError):
    pass


def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _decode(data):
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


def issue(secret, *, issuer, audience, tenant, user, scope, agent=None, run=None, ttl=45):
    if not secret or len(secret) < 32 or not 0 < ttl <= 60:
        raise TokenError('Invalid delegation configuration')
    now = int(time.time())
    claims = {'iss': issuer, 'aud': audience, 'tenant': str(tenant), 'user': str(user),
              'scope': scope, 'agent': str(agent) if agent else None, 'run': str(run) if run else None,
              'iat': now, 'exp': now + ttl, 'jti': uuid4().hex}
    body = _encode(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode())
    signature = _encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return body + '.' + signature


def verify(secret, token, *, issuer, audience, scope, now=None):
    if not secret or len(secret) < 32 or not isinstance(token, str) or len(token) > 4096:
        raise TokenError('Invalid delegation')
    try:
        body, signature = token.split('.')
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            raise TokenError('Invalid signature')
        claims = json.loads(_decode(body))
        current = int(time.time()) if now is None else now
        if (claims.get('iss') != issuer or claims.get('aud') != audience or
                claims.get('scope') != scope or not claims.get('tenant') or not claims.get('user') or
                not claims.get('jti') or type(claims.get('exp')) is not int or
                type(claims.get('iat')) is not int or claims['iat'] > current + 5 or
                claims['exp'] <= current or not 0 < claims['exp'] - claims['iat'] <= 60):
            raise TokenError('Invalid claims')
        return claims
    except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
        raise TokenError('Invalid delegation') from exc

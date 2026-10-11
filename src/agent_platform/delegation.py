"""Rotatable Ed25519 issuer keys and single-use delegations; legacy overlap is explicit."""
import base64
import hashlib
import json
import os
import time
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .db_scope import scope, verified_delegation
from .tokens import TokenError, _decode, _encode, verify


def issue(private_key, *, issuer, kid, tenant, app_id, user, audience='agent-platform', ttl=45):
    if not 0 < ttl <= 60:
        raise TokenError('Invalid lifetime')
    now=int(time.time())
    claims={'iss':issuer,'kid':kid,'aud':audience,'tenant':str(tenant),'app_id':str(app_id),
            'user':str(user),'scope':'agents:access','iat':now,'exp':now+ttl,'jti':uuid4().hex}
    body=_encode(json.dumps(claims,sort_keys=True,separators=(',',':')).encode())
    signature=Ed25519PrivateKey.from_private_bytes(private_key).sign(('v2.'+body).encode())
    return 'v2.'+body+'.'+_encode(signature)


def verify_v2(token, *, keys=None, now=None):
    current=int(time.time()) if now is None else now
    try:
        if len(token)>4096:
            raise ValueError()
        version,body,signature=token.split('.')
        claims=json.loads(_decode(body))
        keyring=keys if keys is not None else json.loads(os.environ.get('AI_DELEGATION_ISSUER_KEYS','[]'))
        selected=[k for k in keyring if k['issuer']==claims['iss'] and k['kid']==claims['kid']
                  and k['not_before']<=current<k['not_after']]
        if version!='v2' or len(selected)!=1:
            raise ValueError()
        key=selected[0]
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key['public_key'],validate=True)).verify(
            _decode(signature),('v2.'+body).encode())
        if (claims['aud']!='agent-platform' or claims['scope']!='agents:access'
            or claims['tenant']!=key['tenant'] or claims['app_id']!=key['app_id']
            or not isinstance(claims['user'],str) or not claims['user'] or len(claims['user'])>200
            or not isinstance(claims['jti'],str) or not 16<=len(claims['jti'])<=128
            or type(claims['iat']) is not int or type(claims['exp']) is not int
            or not 0<claims['exp']-claims['iat']<=60 or claims['iat']>current+5 or claims['exp']<=current):
            raise ValueError()
        claims['legacy_research_access'] = key.get('legacy_research_access') is True
        return claims
    except (ValueError,TypeError,KeyError,InvalidSignature,UnicodeDecodeError):
        raise TokenError('Invalid delegation') from None


async def authenticate(pool, legacy_key, token):
    if token.startswith('v2.'):
        claims=verify_v2(token)
    else:
        cutoff=os.environ.get('AI_DELEGATION_LEGACY_UNTIL','').strip()
        try:
            expired = bool(cutoff) and int(time.time()) >= int(cutoff)
        except ValueError:
            raise TokenError('Invalid legacy retirement configuration') from None
        if expired:
            raise TokenError('Legacy delegation retired')
        claims=verify(legacy_key,token,issuer='nraialgo',audience='agent-platform',scope='agents:access')
    if pool is not None:
        async with pool.connection() as conn,conn.transaction():
            await scope(conn,claims)
            row=await (await conn.execute('''INSERT INTO delegation_replays(issuer,jti,tenant_id,app_id,owner_id,expires_at)
                VALUES(%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING RETURNING jti''',
                (claims['iss'],claims['jti'],claims['tenant'],claims.get('app_id',''),claims['user'],claims['exp']))).fetchone()
            if not row:
                raise TokenError('Delegation already used')
    elif token.startswith('v2.'):
        raise TokenError('Replay storage unavailable')
    return claims


def verified_for(token):
    cached=verified_delegation.get()
    return cached[1] if cached and cached[0]==hashlib.sha256(token.encode()).digest() else None


async def prune_expired(pool):
    """Bounded worker cleanup; never remove an unexpired replay marker."""
    async with pool.connection() as conn:
        await conn.execute("""DELETE FROM delegation_replays WHERE (issuer,jti) IN
            (SELECT issuer,jti FROM delegation_replays WHERE expires_at < now()-interval '5 minutes'
             ORDER BY expires_at LIMIT 1000)""")


def issue_from_file(path, *, user, audience='agent-platform', scope_name='agents:access', agent=None, run=None):
    """Issuer-side helper; key file remains on the consuming backend, never in JS."""
    from pathlib import Path
    file=Path(path)
    if file.is_symlink() or not file.is_file() or file.stat().st_mode & 0o077:
        raise TokenError('Issuer key file must be private (0600)')
    try:
        material=json.loads(file.read_text())
        now=int(time.time())
        if not material['not_before']<=now<material['not_after']:
            raise ValueError()
        claims={'iss':material['issuer'],'kid':material['kid'],'aud':audience,'tenant':material['tenant'],
                'app_id':material['app_id'],'user':str(user),'scope':scope_name,'iat':now,'exp':min(now+45,material['not_after']),
                'jti':uuid4().hex,'agent':str(agent) if agent else None,'run':str(run) if run else None}
        body=_encode(json.dumps(claims,sort_keys=True,separators=(',',':')).encode())
        key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(material['private_key'],validate=True))
        return 'v2.'+body+'.'+_encode(key.sign(('v2.'+body).encode()))
    except (KeyError,ValueError,TypeError):
        raise TokenError('Invalid issuer key configuration') from None

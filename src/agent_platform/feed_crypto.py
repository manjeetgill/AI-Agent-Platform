"""Separate feed keyring; AES-GCM binds each secret to its app, owner and connection."""
import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def keyring():
    current = os.environ.get('AI_FEED_ENCRYPTION_KEY_ID', '')
    keys = {k: base64.b64decode(v, validate=True) for k,v in json.loads(os.environ.get('AI_FEED_ENCRYPTION_KEYS', '{}')).items()}
    if current not in keys or any(len(value)!=32 for value in keys.values()):
        raise ValueError('Feed credential encryption is not configured')
    return current, keys


def context(row):
    return ':'.join(str(row[k]) for k in ('tenant_id', 'app_id', 'owner_id', 'id')).encode()


def seal(secret, row):
    key, keys = keyring()
    nonce = os.urandom(12)
    return json.dumps({'key': key, 'value': base64.b64encode(nonce+AESGCM(keys[key]).encrypt(nonce, secret.encode(), context(row))).decode()})


def unseal(cipher, row):
    _, keys = keyring()
    item = json.loads(cipher)
    data = base64.b64decode(item['value'], validate=True)
    return AESGCM(keys[item['key']]).decrypt(data[:12], data[12:], context(row)).decode()

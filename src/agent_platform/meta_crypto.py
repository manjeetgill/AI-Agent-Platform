"""Authenticated encryption; keys stay in the deployment secret store, never the DB."""
import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def keyring():
    keys = json.loads(os.environ.get('AI_META_ENCRYPTION_KEYS', '{}'))
    current = os.environ.get('AI_META_ENCRYPTION_KEY_ID', '')
    decoded = {name: base64.b64decode(value, validate=True) for name, value in keys.items()}
    if current not in decoded or any(len(value) != 32 for value in decoded.values()):
        raise ValueError('Meta credential encryption is not configured')
    return current, decoded


def seal(value, context):
    current, keys = keyring()
    nonce = os.urandom(12)
    encrypted = AESGCM(keys[current]).encrypt(nonce, value.encode(), context.encode())
    return json.dumps({'key': current, 'value': base64.b64encode(nonce+encrypted).decode()})


def unseal(value, context):
    _, keys = keyring()
    record = json.loads(value)
    raw = base64.b64decode(record['value'], validate=True)
    return AESGCM(keys[record['key']]).decrypt(raw[:12], raw[12:], context.encode()).decode()


def aad(row, kind='token'):
    return ':'.join(map(str, ('meta', row['app_id'], row['owner_id'], row['id'], kind)))

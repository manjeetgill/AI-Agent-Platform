"""Chakra transport primitives. Operator provisioning is required before use.

References: apidocs.chakrahq.com/api-22547477 and /doc-919167.
No automatic retries: a timeout may occur after a message was accepted.
"""
import hashlib
import hmac
import json
import re
from uuid import UUID

import httpx


class ChakraError(Exception):
    """Sanitized provider failure; uncertain sends must be reconciled."""

    def __init__(self, code, *, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


def verify_event(raw, signature, secret, *, phone_id, waba_id):
    """Authenticate the raw pass-through body and enforce channel ownership."""
    if len(raw) > 256000:
        raise ChakraError('WEBHOOK_TOO_LARGE')
    if not secret or len(secret) < 32:
        raise ChakraError('WEBHOOK_SECRET_REQUIRED')
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not re.fullmatch(r'[a-f0-9]{64}', signature):
        raise ChakraError('INVALID_SIGNATURE')
    if not hmac.compare_digest(signature, expected):
        raise ChakraError('INVALID_SIGNATURE')
    try:
        payload = json.loads(raw)
        if payload.get('object') != 'whatsapp_business_account':
            raise ValueError()
        if not isinstance(payload.get('entry'), list) or not payload['entry']:
            raise ValueError()
        for entry in payload['entry']:
            if entry['id'] != waba_id:
                raise ChakraError('WRONG_WHATSAPP_ACCOUNT')
            if not isinstance(entry.get('changes'), list):
                raise TypeError()
            for change in entry['changes']:
                if not isinstance(change, dict) or not isinstance(change.get('value'), dict):
                    raise TypeError()
                if (change.get('field') in {'messages', 'calls'}
                    and change['value'].get('metadata', {}).get('phone_number_id') != phone_id):
                    raise ChakraError('WRONG_PHONE_NUMBER')
        return payload
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ChakraError('INVALID_WEBHOOK_BODY') from None


async def send_message(config, phone_id, payload, *, transport=None):
    """Send once and normalize Chakra's wrapped message ID for the task worker."""
    try:
        plugin = str(UUID(config['plugin_id']))
        version = config['graph_version']
        token = config['access_token']
        if not re.fullmatch(r'v\d+\.\d+', version) or not re.fullmatch(r'\d{5,30}', phone_id):
            raise ValueError()
        if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
            raise ValueError()
        if config['phone_id'] != phone_id:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ChakraError('INVALID_CONNECTION') from None
    url = f'https://api.chakrahq.com/v1/ext/plugin/whatsapp/{plugin}/api/{version}/{phone_id}/messages'
    try:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(12, connect=3),
                                     follow_redirects=False) as client:
            response = await client.post(url, headers={'Authorization': 'Bearer '+token}, json=payload)
    except httpx.HTTPError:
        raise ChakraError('SEND_OUTCOME_UNKNOWN', uncertain=True) from None
    if not 200 <= response.status_code < 300:
        raise ChakraError('PROVIDER_REJECTED', uncertain=response.status_code >= 500)
    try:
        mid = response.json()['_data']['whatsappMessageId']
        if not isinstance(mid, str) or not mid.startswith('wamid.') or len(mid) > 300:
            raise ValueError()
        return mid
    except (ValueError, KeyError, TypeError):
        raise ChakraError('SEND_OUTCOME_UNKNOWN', uncertain=True) from None

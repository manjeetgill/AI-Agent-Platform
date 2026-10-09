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


def _template_connection(config):
    try:
        plugin = str(UUID(config['plugin_id']))
        version, token, waba, phone = (config[k] for k in
                                      ('graph_version', 'access_token', 'waba_id', 'phone_id'))
        if not re.fullmatch(r'v\d+\.\d+', version):
            raise ValueError()
        if not all(re.fullmatch(r'\d{5,30}', value) for value in (waba, phone)):
            raise ValueError()
        if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
            raise ValueError()
        return plugin, version, token, waba, phone
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ChakraError('INVALID_CONNECTION') from None


async def _read(client, url, token, *, params=None):
    try:
        response = await client.get(url, headers={'Authorization': 'Bearer '+token}, params=params)
        if not 200 <= response.status_code < 300:
            raise ChakraError('CATALOGUE_UNAVAILABLE')
        value = response.json()
        if not isinstance(value, dict) or value.get('_errors'):
            raise ValueError()
        value = value.get('_data', value)
        if not isinstance(value, dict):
            raise TypeError()
        return value
    except (httpx.HTTPError, ValueError, TypeError):
        raise ChakraError('CATALOGUE_UNAVAILABLE') from None


async def list_templates(config, *, transport=None):
    """Read the bound WABA catalogue; never follow provider-supplied pagination URLs.

    Contract: https://apidocs.chakrahq.com/api-17615391 (Meta pass-through shape).
    """
    _, version, token, waba, _ = _template_connection(config)
    url = f'https://api.chakrahq.com/v1/ext/plugin/whatsapp/api/{version}/{waba}/message_templates'
    result, seen = [], set()
    params = {'limit': 100}
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(12, connect=3),
                                follow_redirects=False) as client:
        for _ in range(5):
            page = await _read(client, url, token, params=params)
            items, paging = page.get('data'), page.get('paging', {})
            if not isinstance(items, list) or not all(isinstance(t, dict) for t in items):
                raise ChakraError('INVALID_TEMPLATE_CATALOGUE')
            result.extend(items)
            if not isinstance(paging, dict):
                raise ChakraError('INVALID_TEMPLATE_CATALOGUE')
            if not paging.get('next'):
                return result
            cursors = paging.get('cursors', {})
            if not isinstance(cursors, dict):
                raise ChakraError('INVALID_TEMPLATE_CATALOGUE')
            after = cursors.get('after')
            if not isinstance(after, str) or not 1 <= len(after) <= 2048 or after in seen:
                raise ChakraError('INVALID_TEMPLATE_CATALOGUE')
            seen.add(after)
            params = {'limit': 100, 'after': after}
    raise ChakraError('TEMPLATE_CATALOGUE_LIMIT')


async def phone_details(config, *, transport=None):
    """Only return metadata for the phone already bound to this clinic's plugin."""
    plugin, _, token, _, phone = _template_connection(config)
    url = f'https://api.chakrahq.com/v1/ext/plugin/whatsapp/{plugin}/config'
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(12, connect=3),
                                follow_redirects=False) as client:
        data = await _read(client, url, token)
    phones = data.get('whatsappPhoneNumbers', [])
    if not isinstance(phones, list):
        raise ChakraError('INVALID_CONNECTION')
    row = next((p for p in phones if isinstance(p, dict) and p.get('id') == phone), None)
    if not row:
        raise ChakraError('WRONG_PHONE_NUMBER')
    return {'display_phone_number': row.get('display_phone_number', ''),
            'verified_name': row.get('verified_name', '')}


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

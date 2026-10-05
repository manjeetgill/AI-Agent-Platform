"""Server-only, bounded Graph client. Provider payloads never enter errors or logs."""
import hashlib
import hmac
import logging
import os
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .meta_crypto import keyring


class GraphLogRedaction(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if 'graph.facebook.com/' in message:
            record.msg = re.sub(r'(https://graph\.facebook\.com/[^?\s"]+)\?[^\s"]+', r'\1?<redacted>', message)
            record.args = ()
        return True


# httpx logs request URLs at INFO; OAuth query parameters contain one-use secrets.
logging.getLogger('httpx').addFilter(GraphLogRedaction())


class MetaError(Exception):
    def __init__(self, code, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def config(*, signup=True):
    result = {key: os.environ.get('AI_META_'+key.upper(), '') for key in
              ('app_id', 'app_secret', 'configuration_id', 'verify_token', 'public_url')}
    result['graph_version'] = os.environ.get('AI_META_GRAPH_VERSION', 'v26.0')
    url = urlsplit(result['public_url'])
    if (not re.fullmatch(r'\d{5,30}', result['app_id'])
        or (signup and not re.fullmatch(r'\d{5,30}', result['configuration_id']))
        or len(result['app_secret']) < 16 or len(result['verify_token']) < 32
        or url.scheme != 'https' or not url.hostname or url.username or url.password
        or url.path not in ('', '/') or url.query or url.fragment
        or not re.fullmatch(r'v\d+\.\d+', result['graph_version'])):
        raise MetaError('PLATFORM_SETUP_REQUIRED')
    try:
        keyring()
    except (ValueError, KeyError, TypeError):
        raise MetaError('PLATFORM_SETUP_REQUIRED') from None
    result['public_url'] = result['public_url'].rstrip('/')
    return result


class MetaClient:
    def __init__(self, settings=None, transport=None):
        self.settings = settings or config()
        self.transport = transport

    async def request(self, path, token=None, method='GET', params=None, payload=None):
        if not re.fullmatch(r'(oauth/access_token|debug_token|\d{5,30}(/(phone_numbers|register|subscribed_apps|subscriptions))?)', path):
            raise MetaError('INVALID_RESOURCE')
        query = dict(params or {})
        if token:
            query['appsecret_proof'] = hmac.new(self.settings['app_secret'].encode(), token.encode(), hashlib.sha256).hexdigest()
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(12, connect=3)) as client:
                response = await client.request(method,
                    'https://graph.facebook.com/'+self.settings['graph_version']+'/'+path,
                    params=query, json=payload,
                    headers={'Authorization': 'Bearer '+token} if token else {})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise MetaError('PROVIDER_UNAVAILABLE', uncertain=method != 'GET') from None
        if not isinstance(data, dict):
            raise MetaError('PROVIDER_UNAVAILABLE', uncertain=method != 'GET')
        if response.is_error or 'error' in data:
            code = data.get('error', {}).get('code')
            raise MetaError('REAUTH_REQUIRED' if code == 190 else 'META_REQUEST_REJECTED', uncertain=response.status_code >= 500)
        return data

    async def exchange(self, code):
        value = await self.request('oauth/access_token', params={'client_id': self.settings['app_id'],
            'client_secret': self.settings['app_secret'], 'code': code})
        if not isinstance(value.get('access_token'), str):
            raise MetaError('REAUTH_REQUIRED')
        return value['access_token']

    async def validate(self, token, waba=None):
        data = (await self.request('debug_token', self.settings['app_id']+'|'+self.settings['app_secret'],
                                  params={'input_token': token})).get('data', {})
        required = {'whatsapp_business_management', 'whatsapp_business_messaging'}
        if (not data.get('is_valid') or str(data.get('app_id')) != self.settings['app_id']
            or not required <= set(data.get('scopes', []))):
            raise MetaError('REAUTH_REQUIRED')
        now = datetime.now(UTC).timestamp()
        expiries = [v for v in (data.get('expires_at'), data.get('data_access_expires_at')) if isinstance(v, (float, int)) and v > 0]
        if expiries and min(expiries) <= now:
            raise MetaError('REAUTH_REQUIRED')
        if waba:
            targets = {str(t) for s in data.get('granular_scopes', [])
                       if s.get('scope') == 'whatsapp_business_management' for t in s.get('target_ids', [])}
            if not targets or str(waba) not in targets:
                raise MetaError('ASSET_NOT_AUTHORIZED')
        return datetime.fromtimestamp(min(expiries), UTC) if expiries else None

    async def phone(self, token, waba, phone):
        # Read only from the selected WABA; never trust browser asset ownership claims.
        await self.request(waba, token, params={'fields': 'id'})
        after = None
        for _ in range(20):
            params = {'fields': 'id,display_phone_number,verified_name,status,code_verification_status,platform_type', 'limit': 100}
            if after:
                params['after'] = after
            page = await self.request(waba+'/phone_numbers', token, params=params)
            found = next((item for item in page.get('data', []) if item.get('id') == phone), None)
            if found:
                return found
            after = page.get('paging', {}).get('cursors', {}).get('after') if page.get('paging', {}).get('next') else None
            if not after:
                break
        raise MetaError('PHONE_NOT_AUTHORIZED')

    async def subscribed(self, token, waba):
        rows = (await self.request(waba+'/subscribed_apps', token)).get('data', [])
        return any(str(row.get('whatsapp_business_api_data', {}).get('id', row.get('id', ''))) == self.settings['app_id'] for row in rows)

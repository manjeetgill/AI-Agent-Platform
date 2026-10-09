"""Fixed-endpoint, bounded JSON GET transport. No redirect or provider next-URL following."""
import asyncio
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .connector_health import Observation, ProbeResult
from .feed_config import public_address
from .workflow_runtime import select


class FeedFailure(Exception):
    def __init__(self, status, reason, retry=None):
        self.result = ProbeResult(status=status, reason_code=reason, retry_after_seconds=retry)
        super().__init__(reason)


def retry_after(value):
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            seconds = int((parsedate_to_datetime(value)-datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60
    return max(60, min(86400, seconds))


def invalid_constant(value):
    raise ValueError('Non-finite JSON number')


async def fetch(profile, secret, *, reserve, authorized, transport=None, resolver=public_address):
    host = urlsplit(profile.url).hostname
    address = await asyncio.to_thread(resolver, host)
    url = httpx.URL(profile.url).copy_with(host=address)
    headers = {'Host': host, 'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    if profile.auth != 'none':
        if not secret:
            raise FeedFailure('CONFIGURATION_REQUIRED', 'credentials_missing')
        headers['Authorization' if profile.auth == 'bearer' else profile.auth_header] = ('Bearer ' if profile.auth == 'bearer' else '')+secret
    observations, seen = [], set()
    cursor = None
    async with httpx.AsyncClient(transport=transport, timeout=5, follow_redirects=False, trust_env=False) as client:
        for page in range(1, profile.max_pages+1):
            params = {}
            if profile.pagination == 'page':
                params[profile.page_parameter] = str(page)
            elif profile.pagination == 'cursor' and cursor is not None:
                params[profile.page_parameter] = cursor
            document = None
            for attempt in range(3):
                if not await authorized():
                    raise FeedFailure('CONFIGURATION_REQUIRED', 'connection_revoked')
                if not await reserve():
                    raise FeedFailure('RATE_LIMITED', 'app_request_limit', 3600)
                try:
                    async with client.stream('GET', url, params=params, headers=headers,
                                             extensions={'sni_hostname': host}) as response:
                        code = response.status_code
                        if code == 401:
                            raise FeedFailure('LOGIN_REQUIRED', 'credential_rejected')
                        if code == 403 or 300<=code<400:
                            raise FeedFailure('BLOCKED', 'access_denied' if code==403 else 'redirect_not_allowed')
                        if code == 429:
                            # Respect provider cooldown instead of sleeping/retrying in a worker slot.
                            raise FeedFailure('RATE_LIMITED', 'provider_rate_limit', retry_after(response.headers.get('retry-after')))
                        if code >= 500 or code in {408, 425}:
                            if response.headers.get('retry-after'):
                                raise FeedFailure('UNAVAILABLE', 'provider_retry_later', retry_after(response.headers['retry-after']))
                            if attempt == 2:
                                raise FeedFailure('UNAVAILABLE', 'provider_unavailable')
                        elif code != 200:
                            raise FeedFailure('BROKEN', 'unexpected_response')
                        else:
                            media = response.headers.get('content-type', '').split(';')[0].strip()
                            if media != 'application/json' and not media.endswith('+json'):
                                raise FeedFailure('BROKEN', 'json_response_required')
                            if response.headers.get('content-encoding', 'identity') != 'identity':
                                raise FeedFailure('BROKEN', 'encoded_response_not_supported')
                            body = bytearray()
                            async for part in response.aiter_bytes():
                                body.extend(part)
                                if len(body)>262144:
                                    raise FeedFailure('BROKEN', 'response_too_large')
                            if secret and secret in body.decode('utf-8'):
                                raise FeedFailure('BROKEN', 'credential_in_response')
                            document = json.loads(body, parse_constant=invalid_constant)
                            if secret and secret in json.dumps(document):
                                raise FeedFailure('BROKEN', 'credential_in_response')
                            break
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 2:
                        raise FeedFailure('UNAVAILABLE', 'network_timeout') from None
                await asyncio.sleep(0.2*(2**attempt))
            items = select(document, profile.items_pointer)
            if not isinstance(items, list):
                raise FeedFailure('BROKEN', 'items_array_required')
            for item in items:
                values = {key: select(item, pointer) for key,pointer in profile.fields.items()}
                observations.append(Observation.model_validate(values, strict=True))
                if len(observations) >= profile.max_items:
                    return ProbeResult(status='WORKING', reason_code='item_limit_reached', observations=observations)
            if profile.pagination == 'none':
                break
            cursor = select(document, profile.next_pointer)
            if cursor is None or cursor is False or cursor == '':
                break
            if profile.pagination == 'page':
                if cursor is not True:
                    raise FeedFailure('BROKEN', 'has_more_boolean_required')
            else:
                if not isinstance(cursor, str) or len(cursor)>256 or cursor in seen:
                    raise FeedFailure('BROKEN', 'invalid_or_repeated_cursor')
                seen.add(cursor)
            if page == profile.max_pages:
                return ProbeResult(status='WORKING', reason_code='page_limit_reached', observations=observations)
    return ProbeResult(status='WORKING', reason_code='feed_read', observations=observations)

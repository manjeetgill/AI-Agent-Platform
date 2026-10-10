"""Traccar direct-phone adapter; no public relay, device deduplication or delivery claim."""
import asyncio

import httpx

from .sms_config import traccar_key, validate_gateway_url


async def request(settings, payload, *, transport=None):
    base = validate_gateway_url(settings.gateway_url, settings.allow_lan_http)
    async with asyncio.timeout(15), httpx.AsyncClient(timeout=10, trust_env=False,
            follow_redirects=False, transport=transport) as client, client.stream(
                'POST', base+'/', headers={'Authorization':traccar_key(settings)}, json=payload) as response:
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 64000:
                raise ValueError('Gateway response too large')
        return response.status_code, bytes(raw)


DIAGNOSTICS = {
    'traccar_permission_error': 'Phone reported a permission error. Check SMS and phone/SIM permissions for Traccar; delivery remains unconfirmed.',
    'traccar_sim_error': 'Phone reported a SIM/subscription error. Check that the selected SIM is active and Traccar can read phone state.',
    'traccar_request_format_error': 'Gateway reported a request parsing error. Check the installed Traccar version supports the slot field.',
    'traccar_server_error': 'Phone returned a server error without a recognized cause. Check Traccar on the phone; do not retry until delivery is checked.',
    'traccar_outcome_unknown': 'Gateway response did not confirm acceptance. Earlier raw responses were not retained; no automatic resend.',
    'traccar_transport_outcome_unknown': 'Connection failed after sending may have begun. Check the phone before any resend.',
    'connection_not_established': 'Could not connect to the phone. Check its local service, Wi-Fi and local-network permission.',
    'traccar_authentication_rejected': 'Local service rejected the credential. Check its local token, not the cloud token.',
}


def diagnostic(code):
    return DIAGNOSTICS.get(code)


def failure_code(status, raw):
    # Classify locally, then discard raw text: Android errors can contain secrets or SMS text.
    text = raw.decode('utf-8', errors='replace').lower()
    if any(word in text for word in ('send_sms', 'read_phone_state', 'permission denial', 'permission denied', 'securityexception')):
        return 'traccar_permission_error'
    if any(word in text for word in ('subscriptioninfo', 'subscriptionid', 'subscription id', 'sim slot', 'invalid subscription')):
        return 'traccar_sim_error'
    if any(word in text for word in ('jsonreader', 'malformedjson', 'expected a name', 'expected an int', 'unexpected token')):
        return 'traccar_request_format_error'
    return 'traccar_server_error' if status >= 500 else 'traccar_outcome_unknown'


async def check_connection(settings, *, transport=None):
    """Empty object exercises authentication/validation without a recipient or message.

    Official GatewayServer returns 500 + this exact text without calling the
    SMS handler. GET is public HTML and cannot validate an API key.
    """
    try:
        status, raw = await request(settings, {}, transport=transport)
        if status == 500 and raw == b'Missing phone or message':
            # No recipient or message in either probe; official handler cannot invoke SMS.
            slot_status, slot_raw = await request(settings, {'slot': settings.sim_slot-1}, transport=transport)
            supported = slot_status == 500 and slot_raw == b'Missing phone or message'
            code = 'authenticated_validation_available' if supported else failure_code(slot_status, slot_raw)
            return {'connected': True, 'code': code, 'slot_parser_supported': supported,
                    'sim_send_verified': False, 'diagnostic': diagnostic(code), 'sms_sent': False}
        return {'connected': False, 'code': 'authentication_failed' if status in {401,403} else 'unexpected_gateway_response', 'sms_sent': False}
    except Exception:  # noqa: BLE001 -- never expose private transport errors
        return {'connected': False, 'code': 'gateway_unreachable_or_invalid', 'sms_sent': False}


class TraccarLocal:
    async def send(self, settings, sms, operation, *, transport=None):
        from .sms_providers import Outcome
        if sms['to'] not in settings.test_recipients or sms['segments'] != 1:
            return Outcome('FAILED', 'traccar_pilot_recipient_or_segment_blocked')
        # Traccar accepts only to/message/slot; slot is zero-based, platform UI is one-based.
        # Do not send unsupported id/TTL fields or rely on device idempotency.
        payload = {'to':'+'+sms['to'], 'message':sms['message'], 'slot':settings.sim_slot-1}
        try:
            status, raw = await request(settings, payload, transport=transport)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return Outcome('RETRY', 'connection_not_established')
        except (httpx.HTTPError, TimeoutError, ValueError):
            return Outcome('OUTCOME_UNKNOWN', 'traccar_transport_outcome_unknown', uncertain=True)
        if status in {401,403}:
            return Outcome('FAILED', 'traccar_authentication_rejected', http_status=status)
        if 300 <= status < 400:
            return Outcome('FAILED', 'traccar_redirect_rejected', http_status=status)
        # Empty HTTP 200 means Android send API was invoked; no message ID or receipt.
        if status == 200 and raw == b'':
            return Outcome('ACCEPTED', http_status=status)
        return Outcome('OUTCOME_UNKNOWN', failure_code(status, raw), http_status=status, uncertain=True)

"""Bounded SMSIndiaHub POST adapter. No credentials in URLs or response bodies in logs."""
import asyncio
import re
from dataclasses import dataclass

import httpx

from .email_providers import Outcome as BaseOutcome
from .email_providers import retry_after
from .sms_config import api_key
from .sms_traccar import TraccarLocal


@dataclass(frozen=True)
class Outcome(BaseOutcome):
    provider_job_id: str | None = None


class Mock:
    async def send(self, settings, sms, operation, *, transport=None):
        return Outcome('MOCK_SENT', provider_id='mock-'+operation)


class SMSIndiaHub:
    async def send(self, settings, sms, operation, *, transport=None):
        payload = {'Account.APIKey':api_key(settings), 'SenderId':sms['sender_id'],
                   'Message':sms['message'], 'Channel':'2', 'DCS':sms['dcs'], 'FlashSMS':'0',
                   'Number':sms['to'], 'Route':'1', 'PEId':sms['pe_id'], 'TemplateId':sms['template_id']}
        try:
            async with asyncio.timeout(15), httpx.AsyncClient(timeout=10, follow_redirects=False,
                    trust_env=False, transport=transport) as client, client.stream('POST', 'https://cloud.smsindiahub.in/api/mt/SendSMS', json=payload) as response:
                status = response.status_code
                if status == 429:
                    return Outcome('RETRY', 'provider_rate_limited', http_status=status, retry_after=retry_after(response))
                if status >= 500 or status == 408:
                    return Outcome('OUTCOME_UNKNOWN', 'provider_outcome_unknown', http_status=status, uncertain=True)
                if not 200 <= status < 300:
                    return Outcome('FAILED', 'provider_rejected', http_status=status)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 64000:
                        return Outcome('OUTCOME_UNKNOWN', 'provider_response_unrecognized', http_status=status, uncertain=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return Outcome('RETRY', 'connection_not_established')
        except (httpx.HTTPError, TimeoutError):
            return Outcome('OUTCOME_UNKNOWN', 'transport_outcome_unknown', uncertain=True)
        try:
            import json
            data = json.loads(raw)
            code = data.get('ErrorCode')
            if code != '000':
                if isinstance(code, str) and re.fullmatch(r'[0-9]{3}', code):
                    return Outcome('FAILED', 'provider_rejected', http_status=status)
                raise ValueError()
            records = data['MessageData']
            if not isinstance(records, list) or len(records) != 1 or records[0].get('Number') != sms['to']:
                raise ValueError()
            job, message = data['JobId'], records[0]['MessageId']
            if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', v) for v in (job,message)):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            return Outcome('OUTCOME_UNKNOWN', 'provider_response_unrecognized', http_status=status, uncertain=True)
        # DeliveryDate in a send response is NOT a verified handset delivery receipt.
        return Outcome('ACCEPTED', provider_id=message, provider_job_id=job, http_status=status)


ADAPTERS = {'mock':Mock(), 'smsindiahub':SMSIndiaHub(), 'traccar_local':TraccarLocal()}

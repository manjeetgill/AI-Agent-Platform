"""Provider adapters with redacted outcomes. Acceptance is never inbox delivery."""
import asyncio
import re
from dataclasses import dataclass

import httpx

from .email_config import api_key
from .email_smtp import SMTPAdapter


@dataclass(frozen=True)
class Outcome:
    status: str
    code: str | None = None
    provider_id: str | None = None
    http_status: int | None = None
    retry_after: int = 0
    uncertain: bool = False


def retry_after(response):
    try:
        return max(1,min(86400,int(response.headers.get('retry-after','30'))))
    except ValueError:
        return 60


class Mock:
    async def send(self, settings, email, operation, *, transport=None):
        return Outcome('MOCK_SENT', provider_id='mock-'+operation)


class Resend:
    async def send(self, settings, email, operation, *, transport=None):
        payload={'from':email['from_name']+' <'+email['from_email']+'>','to':[email['to']],
                 'subject':email['subject'],'html':email['html'],'text':email['text']}
        return await send_http('resend','https://api.resend.com/emails',
            {'Authorization':'Bearer '+api_key(settings),'Idempotency-Key':'notification/'+operation},payload,transport)


class Brevo:
    async def send(self, settings, email, operation, *, transport=None):
        payload={'sender':{'email':email['from_email'],'name':email['from_name']},'to':[{'email':email['to']}],
                 'subject':email['subject'],'htmlContent':email['html'],'textContent':email['text']}
        # Single-email ambiguity is not replayed: batch-specific idempotency guarantees
        # are not assumed for this endpoint usage.
        return await send_http('brevo','https://api.brevo.com/v3/smtp/email',{'api-key':api_key(settings)},payload,transport)


async def send_http(provider, url, headers, payload, transport):
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False,transport=transport) as client:
            response=await client.post(url,headers=headers,json=payload)
    except (httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout):
        return Outcome('RETRY','connection_not_established')
    except (httpx.HTTPError,TimeoutError):
        return Outcome('RETRY' if provider=='resend' else 'OUTCOME_UNKNOWN','transport_outcome_unknown',uncertain=True)
    status=response.status_code
    if status==429:
        return Outcome('RETRY','provider_rate_limited',http_status=status,retry_after=retry_after(response))
    if status>=500 or status==408:
        return Outcome('RETRY' if provider=='resend' else 'OUTCOME_UNKNOWN','provider_outcome_unknown',http_status=status,uncertain=True)
    if not 200<=status<300:
        return Outcome('FAILED','provider_rejected',http_status=status)
    try:
        identifier=response.json().get('id' if provider=='resend' else 'messageId')
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_@.<>:+/=-]{1,300}',identifier):
            raise ValueError()
    except (ValueError,AttributeError):
        return Outcome('OUTCOME_UNKNOWN','provider_response_unrecognized',http_status=status,uncertain=True)
    return Outcome('ACCEPTED',provider_id=identifier,http_status=status)


ADAPTERS={'mock':Mock(),'resend':Resend(),'brevo':Brevo(),'smtp':SMTPAdapter()}

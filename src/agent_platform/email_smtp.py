"""Operator-owned SMTP submission. Never replay an ambiguous DATA result."""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from uuid import UUID

from .email_config import api_key


def submit(settings, email, operation):
    from .email_providers import Outcome

    config = settings.smtp
    message = EmailMessage(policy=SMTP)
    message['From'] = f"{email['from_name']} <{email['from_email']}>"
    message['To'] = email['to']
    message['Subject'] = email['subject']
    # Correlation only: SMTP servers do NOT guarantee deduplication by Message-ID.
    identifier = f"<{UUID(operation)}@{email['from_email'].split('@')[1]}>"
    message['Message-ID'] = identifier
    message['Date'] = formatdate(usegmt=True)
    message.set_content(email['text'])
    message.add_alternative(email['html'], subtype='html')
    raw = message.as_bytes()
    client = None
    data_started = False
    try:
        if config.security == 'tls':
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=3, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=3)
        client.ehlo_or_helo_if_needed()
        if config.security == 'starttls':
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if config.username:
            client.login(config.username, api_key(settings))
        for command, address in [(client.mail, email['from_email']), (client.rcpt, email['to'])]:
            code, _ = command(address)
            if not 200 <= code < 300:
                return Outcome('RETRY' if 400 <= code < 500 else 'FAILED', 'smtp_envelope_rejected')
        data_started = True
        code, _ = client.data(raw)
        if code != 250:
            return Outcome('RETRY' if 400 <= code < 500 else 'FAILED', 'smtp_data_rejected')
        return Outcome('ACCEPTED', provider_id=identifier)
    except smtplib.SMTPAuthenticationError:
        return Outcome('FAILED', 'smtp_authentication_failed')
    except smtplib.SMTPDataError as exc:
        # Explicit negative DATA reply confirms non-acceptance.
        return Outcome('RETRY' if 400 <= exc.smtp_code < 500 else 'FAILED', 'smtp_data_rejected')
    except (ssl.SSLError, smtplib.SMTPNotSupportedError):
        return Outcome('FAILED', 'smtp_security_configuration')
    except (OSError, smtplib.SMTPException):
        return Outcome('OUTCOME_UNKNOWN' if data_started else 'RETRY',
                       'smtp_outcome_unknown' if data_started else 'smtp_connection_failed', uncertain=data_started)
    finally:
        if client:
            # QUIT is unnecessary after acceptance and must not obscure a successful DATA reply.
            client.close()


class SMTPAdapter:
    async def send(self, settings, email, operation, *, transport=None):
        from .email_providers import Outcome

        try:
            return await asyncio.wait_for(asyncio.to_thread(submit, settings, email, operation), timeout=25)
        except TimeoutError:
            # The bounded socket operation may still finish; never send a second copy.
            return Outcome('OUTCOME_UNKNOWN', 'smtp_timeout', uncertain=True)

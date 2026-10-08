"""Backend-only client for an app's WhatsApp notification integration."""

from urllib.parse import urlsplit

import httpx


class WhatsAppNotifications:
    def __init__(self, base_url: str, service_key: str):
        url = urlsplit(base_url)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('Use the platform HTTPS origin')
        self.client = httpx.Client(base_url=base_url,
                                   headers={'Authorization': 'Bearer ' + service_key},
                                   timeout=httpx.Timeout(20, connect=3), follow_redirects=False)

    def request(self, owner_id: str, method: str, path: str, body=None):
        # owner_id comes from your authenticated app session, never directly from a public form.
        response = self.client.request(method, '/v1/whatsapp/alerts/' + path,
                                       headers={'X-External-User-ID': owner_id}, json=body)
        response.raise_for_status()
        return response.json() if response.content else None

    def bind_owned_sender(self, owner_id: str, channel_id: str, daily_limit: int = 20):
        return self.request(owner_id, 'PUT', 'route',
                            {'channel_id': channel_id, 'daily_limit': daily_limit})

    def route(self, owner_id: str):
        return self.request(owner_id, 'GET', 'route')

    def begin_recipient_verification(self, owner_id: str, recipient: str):
        return self.request(owner_id, 'POST', 'verifications', {'recipient': recipient})

    def verification(self, owner_id: str, verification_id: str):
        return self.request(owner_id, 'GET', 'verifications/' + verification_id)

    def subscribe(self, owner_id: str, recipient: str):
        return self.request(owner_id, 'PUT', 'subscription',
                            {'recipient': recipient, 'consented': True})

    def notify(self, owner_id: str, payload: dict):
        # Use a durable event ID as request_key. Reconcile by GET before retrying uncertain requests.
        return self.request(owner_id, 'POST', 'notifications', payload)

    def delivery(self, owner_id: str, delivery_id: str):
        return self.request(owner_id, 'GET', 'deliveries/' + delivery_id)

    def close(self):
        self.client.close()

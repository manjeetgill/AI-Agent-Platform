"""Use on the consuming app's BACKEND, never ship its service key to a browser."""

from urllib.parse import urlsplit

import httpx


class TelephoneAgents:
    def __init__(self, base_url, service_key):
        url = urlsplit(base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("Use the platform HTTPS origin")
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": "Bearer " + service_key},
            timeout=httpx.Timeout(25, connect=3),
            follow_redirects=False,
        )

    def request(self, owner_id, method, path, body=None):
        # Map owner_id from your authenticated app session; never trust a browser-supplied owner.
        response = self.client.request(
            method, "/v1/telephony/" + path, headers={"X-External-User-ID": owner_id}, json=body
        )
        response.raise_for_status()
        return response.json()  # No automatic retries for mutations.

    def prepare(self, owner_id, *, external_id, agent_id, number):
        # Save returned gateway_key once in the app's secret manager, not logs or browser storage.
        return self.request(
            owner_id,
            "POST",
            "connections",
            {"external_id": external_id, "agent_id": agent_id, "number": number},
        )

    def readiness(self, owner_id, connection_id):
        return self.request(owner_id, "GET", f"connections/{connection_id}/readiness")

    def activate(self, owner_id, connection_id, enabled=True):
        return self.request(owner_id, "PATCH", f"connections/{connection_id}", {"enabled": enabled})

    def calls(self, owner_id, connection_id):
        return self.request(owner_id, "GET", f"connections/{connection_id}/calls")

    def configure_tools(self, owner_id, connection_id, endpoint, signing_secret, tools):
        return self.request(
            owner_id,
            "PUT",
            f"connections/{connection_id}/tools",
            {"endpoint": endpoint, "secret": signing_secret, "tools": tools},
        )

    def authorize_caller(self, owner_id, connection_id, call_id, verified_subject, tool_names):
        # Call ONLY after your backend verifies both the customer identity and this call's association.
        return self.request(
            owner_id,
            "POST",
            f"connections/{connection_id}/calls/{call_id}/authorize",
            {"subject": verified_subject, "tools": tool_names},
        )

    def actions(self, owner_id, connection_id, call_id):
        return self.request(owner_id, "GET", f"connections/{connection_id}/calls/{call_id}/actions")

    def reconcile(self, owner_id, connection_id, call_id, action_id):
        return self.request(
            owner_id, "POST", f"connections/{connection_id}/calls/{call_id}/actions/{action_id}/reconcile"
        )

    def close(self):
        self.client.close()

"""Backend-only calling client. Credentials must never be sent to a browser."""

import httpx


class CallingClient:
    def __init__(self, base_url, credential, external_user_id):
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=15,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + credential, "X-External-User-ID": external_user_id},
        )

    def close(self):
        self.client.close()

    def _request(self, method, path, **kwargs):
        response = self.client.request(method, "/v1/calling" + path, **kwargs)
        response.raise_for_status()
        return response.json()

    def create(self, external_id, name, greeting, handoff_message, connection_ref=None, language="en-IN"):
        return self._request(
            "POST",
            "/agents",
            json={
                "external_id": external_id,
                "name": name,
                "greeting": greeting,
                "handoff_message": handoff_message,
                "connection_ref": connection_ref,
                "language": language,
            },
        )

    def setup(self, agent_id):
        return self._request("GET", f"/agents/{agent_id}/setup")

    def preview(self, agent_id):
        return self._request("POST", f"/agents/{agent_id}/preview")

    def activate(self, agent_id, enabled):
        return self._request("POST", f"/agents/{agent_id}/activation", json={"enabled": enabled})

    def calls(self, agent_id, before=None):
        return self._request("GET", f"/agents/{agent_id}/calls", params={"before": before} if before else {})

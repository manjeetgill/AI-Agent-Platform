"""Wizard facade, private credential storage and activation gates."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from test_task_agents import database, sql

from agent_platform.app import create_app
from agent_platform.console_app import create_console


def test_whatsapp_setup_and_inbox_console(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PLATFORM_DATABASE_URL", database())
    monkeypatch.setenv("AI_WHATSAPP_CONNECTIONS", "{}")
    secret_file = tmp_path / "connections.json"
    monkeypatch.setenv("AI_WHATSAPP_CONNECTIONS_FILE", str(secret_file))
    app = create_console(app=create_app(signing_key="c" * 48))
    headers = {"X-Console-Intent": "user-action"}
    phone = str(uuid4().int)[:20]
    body = {
        "phone_id": phone,
        "graph_version": "v23.0",
        "access_token": "fixture-token-only",
        "app_secret": "fixture-app-secret",
        "verify_token": "fixture-verify-token",
    }
    with TestClient(app) as client:
        assert client.get("/whatsapp").status_code == 200
        assert client.get("/console/whatsapp.js").status_code == 200
        assert client.get("/whatsapp", headers={"Host": "outside.example"}).status_code == 403
        endpoint = "/console/whatsapp/connection"
        assert client.post(endpoint, json=body).status_code == 403
        assert (
            client.post(
                endpoint, headers={**headers, "Origin": "https://outside.example"}, json=body
            ).status_code
            == 403
        )
        invalid = client.post(endpoint, headers=headers, json={**body, "phone_id": "bad"})
        assert invalid.status_code == 422 and "fixture-app-secret" not in invalid.text
        result = client.post(endpoint, headers=headers, json=body)
        assert result.status_code == 200 and "fixture-token-only" not in result.text
        assert secret_file.stat().st_mode & 0o077 == 0
        assert json.loads(secret_file.read_text())[phone]["app_id"] == app.state.console_app_id
        setup = client.get("/console/api/whatsapp/setup", headers=headers)
        assert setup.json()["phone_ids"] == [phone]
        assert "fixture-token-only" not in setup.text
        document = client.post(
            "/console/api/knowledge/documents",
            headers=headers,
            json={
                "external_id": uuid4().hex,
                "title": "Public FAQ",
                "text": "Store opens at nine.",
                "visibility": "app",
            },
        ).json()
        channel = client.post(
            "/console/api/whatsapp/channels",
            headers=headers,
            json={"name": "Support fixture", "phone_id": phone, "documents": [document["id"]]},
        ).json()
        try:
            path = "/console/api/whatsapp/channels/" + channel["id"]
            assert (
                client.post(path + "/activation", headers=headers, json={"enabled": True}).status_code == 409
            )
            assert client.get(path + "/conversations", headers=headers).json() == []
            assert (
                client.get(
                    "/webhooks/whatsapp/" + channel["id"],
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": body["verify_token"],
                        "hub.challenge": "verified",
                    },
                ).text
                == "verified"
            )
            sql(
                "INSERT INTO whatsapp_worker_health VALUES('worker',now(),true) ON CONFLICT(id) DO UPDATE SET last_seen=now(),model_configured=true"
            )
            assert (
                client.post(path + "/activation", headers=headers, json={"enabled": True}).status_code == 200
            )
            # Updating secrets invalidates verification and stops automatic dispatch.
            assert client.post(endpoint, headers=headers, json=body).status_code == 200
            row = sql(
                "SELECT enabled,webhook_verified_at FROM whatsapp_channels WHERE id=%s", (channel["id"],)
            )[0]
            assert not row["enabled"] and row["webhook_verified_at"] is None
        finally:
            sql("DELETE FROM whatsapp_channels WHERE id=%s", (channel["id"],))
            sql("DELETE FROM knowledge_documents WHERE id=%s", (document["id"],))

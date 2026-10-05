"""Credential entry only in the loopback operator console; never a public onboarding endpoint."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import Field, SecretStr

from .tasks import Strict
from .whatsapp import connection_configs


class ConnectionForm(Strict):
    waba_id: str | None = Field(default=None, pattern=r"^\d{5,30}$")
    phone_id: str = Field(pattern=r"^\d{5,30}$")
    graph_version: str = Field(pattern=r"^v\d+\.\d+$")
    access_token: SecretStr = Field(min_length=10, max_length=4096)
    app_secret: SecretStr = Field(min_length=16, max_length=256)
    verify_token: SecretStr = Field(min_length=16, max_length=256)


def install_connection_form(app, local):
    lock = asyncio.Lock()

    @app.post("/console/whatsapp/connection")
    async def save_connection(request: Request):
        local(request, mutation=True)
        # Parse manually so validation errors never echo submitted secret fields.
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 16000:
                raise HTTPException(413, "Connection settings too large")
        try:
            body = ConnectionForm.model_validate_json(raw)
        except ValueError:
            raise HTTPException(422, "Check number ID, Graph version and all three secret fields") from None
        filename = os.environ.get("AI_WHATSAPP_CONNECTIONS_FILE")
        if not filename:
            raise HTTPException(503, "Operator must configure a private credential file before saving")
        async with lock:
            values = connection_configs()
            app_id = str(app.state.console_app_id)
            previous = values.get(body.phone_id)
            if previous and previous.get("app_id") != app_id:
                raise HTTPException(409, "Number is assigned to a different application")
            path = Path(filename)
            if not path.parent.is_dir():
                raise HTTPException(503, "Private credential directory is missing")
            values[body.phone_id] = {
                "app_id": app_id,
                "graph_version": body.graph_version,
                "access_token": body.access_token.get_secret_value(),
                "app_secret": body.app_secret.get_secret_value(),
                "verify_token": body.verify_token.get_secret_value(),
                "waba_id": body.waba_id or (previous or {}).get("waba_id"),
            }
            async with app.state.repository.pool.connection() as conn:
                await conn.execute(
                    "UPDATE whatsapp_channels SET enabled=false,webhook_verified_at=NULL WHERE phone_id=%s AND app_id=%s",
                    (body.phone_id, app_id),
                )
            # Atomic private file, outside version control. No credentials in responses/logs.
            fd, temporary = tempfile.mkstemp(prefix=".whatsapp-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(values, stream)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {"saved": True, "phone_id": body.phone_id}

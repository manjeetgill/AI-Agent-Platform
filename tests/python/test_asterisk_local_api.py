import asyncio
from uuid import uuid4

import httpx

from agent_platform.asterisk_local_api import create_gateway


def test_gateway_binds_one_agent_and_requires_private_credential():
    async def run():
        calls = []
        settings = {"agent_id": str(uuid4()), "service_key": "test-key", "external_user_id": "operator"}

        async def backend(request):
            calls.append(request)
            return httpx.Response(200, json={"ready_to_attempt": True})

        app = create_gateway(settings, transport=httpx.MockTransport(backend))
        prefix = "/v1/voice/agents/" + settings["agent_id"]
        headers = {"Authorization": "Bearer test-key", "X-External-User-ID": "operator"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://localhost"
        ) as client:
            assert (await client.get(prefix + "/readiness")).status_code == 401
            assert (
                await client.get(prefix + "/readiness", headers={**headers, "X-External-User-ID": "other"})
            ).status_code == 401
            assert (await client.get("/console", headers=headers)).status_code == 404
            assert (
                await client.post("/v1/voice/agents/" + str(uuid4()) + "/preview", headers=headers)
            ).status_code == 404
            assert (await client.post(prefix + "/settings", headers=headers)).status_code == 404
            assert (
                await client.post(prefix + "/preview", headers=headers, content=b"x" * 16385)
            ).status_code == 413
            assert calls == []
            assert (await client.get(prefix + "/readiness", headers=headers)).status_code == 200
            assert len(calls) == 1
            assert calls[0].url.host == "127.0.0.1"
            assert calls[0].headers["Authorization"] == "Bearer test-key"

    asyncio.run(run())


def test_gateway_times_out_without_retry():
    async def run():
        count = 0

        async def backend(_request):
            nonlocal count
            count += 1
            raise httpx.ReadTimeout("sensitive upstream details")

        settings = {"agent_id": str(uuid4()), "service_key": "test-key", "external_user_id": "operator"}
        app = create_gateway(settings, transport=httpx.MockTransport(backend))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://localhost"
        ) as client:
            response = await client.post(
                "/v1/voice/agents/" + settings["agent_id"] + "/preview",
                headers={"Authorization": "Bearer test-key", "X-External-User-ID": "operator"},
                json={},
            )
            assert response.status_code == 503
            assert response.content == b""
            assert count == 1

    asyncio.run(run())


def test_local_certificate_passes_strict_tls_validation(tmp_path, monkeypatch):
    import ssl
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from asterisk_connect_local import certificates

    certificates(tmp_path)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp_path / "server-cert.pem", tmp_path / "server-key.pem")
    client = ssl.create_default_context(cafile=str(tmp_path / "platform-ca.pem"))
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
    c = client.wrap_bio(ci, co, server_hostname="host.docker.internal")
    s = server.wrap_bio(si, so, server_side=True)
    finished = set()
    for _ in range(10):
        for label, connection in [("client", c), ("server", s)]:
            try:
                connection.do_handshake()
                finished.add(label)
            except ssl.SSLWantReadError:
                pass
        if co.pending:
            si.write(co.read())
        if so.pending:
            ci.write(so.read())
        if len(finished) == 2:
            break
    assert finished == {"client", "server"}


def test_byoc_local_gateway_never_exposes_management_or_other_connection():
    async def run():
        connection = str(uuid4())
        settings = {'connection_id': connection, 'gateway_key': 'tgw_' + 'x'*48}
        calls = []
        async def handler(request):
            calls.append(request)
            assert 'x-external-user-id' not in request.headers
            return httpx.Response(200, json={'connected': True})
        app = create_gateway(settings, transport=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://local') as c:
            prefix = f'/v1/telephony/gateway/{connection}'
            h = {'Authorization': 'Bearer ' + settings['gateway_key']}
            assert (await c.post(prefix+'/heartbeat', headers=h)).status_code == 200
            assert (await c.post(prefix+'/heartbeat')).status_code == 401
            assert (await c.post('/v1/telephony/connections', headers=h)).status_code == 404
            assert (await c.post(f'/v1/telephony/gateway/{uuid4()}/heartbeat', headers=h)).status_code == 404
            assert len(calls) == 1
    asyncio.run(run())

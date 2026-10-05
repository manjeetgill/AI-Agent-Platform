import asyncio

import httpx
import pytest

from agent_platform.calling_readiness import provider_readiness

CFG = {"account_sid": "AC" + "a" * 32, "auth_token": "private-token", "number": "+15551234567"}
URLS = {
    "inbound": "https://agents.example/webhooks/calling/a/inbound",
    "status": "https://agents.example/webhooks/calling/a/status",
}
ROW = {
    "account_sid": CFG["account_sid"],
    "phone_number": CFG["number"],
    "capabilities": {"voice": True},
    "voice_url": URLS["inbound"],
    "voice_method": "POST",
    "status_callback": URLS["status"],
    "status_callback_method": "POST",
    "voice_application_sid": None,
    "trunk_sid": None,
}


@pytest.mark.parametrize(
    "patch,code",
    [
        ({}, "ready"),
        ({"account_sid": "AC" + "b" * 32}, "number_missing"),
        ({"phone_number": "+15559999999"}, "number_missing"),
        ({"capabilities": {"voice": False}}, "voice_disabled"),
        ({"voice_method": "GET"}, "routing"),
        ({"status_callback": "https://wrong.example"}, "routing"),
        ({"voice_application_sid": "AP" + "a" * 32}, "routing"),
        ({"trunk_sid": "TK" + "a" * 32}, "routing"),
    ],
)
def test_number_and_routing_checks(patch, code):
    def handler(req):
        assert req.url.host == "api.twilio.com"
        assert req.url.params["PhoneNumber"] == CFG["number"]
        assert req.method == "GET"
        return httpx.Response(200, json={"incoming_phone_numbers": [{**ROW, **patch}]})

    r = asyncio.run(provider_readiness(CFG, URLS, transport=httpx.MockTransport(handler)))
    assert r["code"] == code
    assert r["ready"] == (code == "ready")
    assert "private-token" not in str(r)


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "credentials"),
        (403, "credentials"),
        (429, "unavailable"),
        (500, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_errors_fail_closed_without_credentials(status, code):
    r = asyncio.run(
        provider_readiness(
            CFG, URLS, transport=httpx.MockTransport(lambda _: httpx.Response(status, text="private-token"))
        )
    )
    assert r["code"] == code
    assert "private-token" not in str(r)


def test_caller_id_does_not_prove_inbound_ownership():
    r = asyncio.run(
        provider_readiness(
            CFG,
            URLS,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"incoming_phone_numbers": []})),
        )
    )
    assert r["code"] == "number_missing"

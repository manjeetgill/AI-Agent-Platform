"""Read-only provider checks. A saved number or verified caller ID is not inbound routing."""

import re

import httpx

MESSAGES = {
    "ready": "Provider number and webhook routing are ready.",
    "credentials": "Your setup team must reconnect the voice-provider account.",
    "number_missing": "This number is not an incoming voice number in the connected provider account.",
    "voice_disabled": "The provider number does not support incoming voice calls.",
    "routing": "Your setup team must point the provider voice and status webhooks to this agent.",
    "unavailable": "Voice provider unavailable. Check again before enabling calls.",
}


async def provider_readiness(cfg, callbacks, *, transport=None):
    """Check the account-owned incoming number, never a caller-ID verification resource."""
    if not re.fullmatch(r"AC[a-fA-F0-9]{32}", cfg.get("account_sid", "")):
        return result("credentials")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(4, connect=2), follow_redirects=False, transport=transport
        ) as client:
            response = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{cfg['account_sid']}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": cfg["number"], "PageSize": 20},
                auth=httpx.BasicAuth(cfg["account_sid"], cfg["auth_token"]),
            )
        if response.status_code in (401, 403):
            return result("credentials")
        response.raise_for_status()
        if len(response.content) > 262144:
            return result("unavailable")
        rows = response.json().get("incoming_phone_numbers", [])
        matches = [
            r
            for r in rows
            if r.get("phone_number") == cfg["number"] and r.get("account_sid") == cfg["account_sid"]
        ]
        if len(matches) != 1:
            return result("number_missing")
        row = matches[0]
        if row.get("capabilities", {}).get("voice") is not True:
            return result("voice_disabled")
        if (
            row.get("voice_url") != callbacks["inbound"]
            or row.get("voice_method") != "POST"
            or row.get("status_callback") != callbacks["status"]
            or row.get("status_callback_method") != "POST"
            or row.get("voice_application_sid")
            or row.get("trunk_sid")
        ):
            return result("routing")
        return result("ready")
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        # Provider error bodies and authenticated URLs must never enter logs or API responses.
        return result("unavailable")


def result(code):
    return {"ready": code == "ready", "code": code, "message": MESSAGES[code]}

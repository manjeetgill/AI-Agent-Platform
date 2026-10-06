"""WhatsApp Calling API transport through an existing Meta or Chakra connection."""

import re
import time

import httpx


class CallingError(Exception):
    def __init__(self, code, *, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


async def request(config, phone_id, resource, *, method="GET", payload=None, params=None, transport=None):
    if not re.fullmatch(r"\d{5,30}", phone_id) or not re.fullmatch(
        r"v\d+\.\d+", config.get("graph_version", "")
    ):
        raise CallingError("INVALID_CONNECTION")
    if resource not in {"settings", "call_permissions", "calls", "messages"}:
        raise CallingError("INVALID_RESOURCE")
    provider = config.get("provider", "meta")
    if provider == "chakra":
        url = f"https://api.chakrahq.com/v1/whatsapp/{config['graph_version']}/{phone_id}/{resource}"
    elif provider == "meta":
        url = f"https://graph.facebook.com/{config['graph_version']}/{phone_id}/{resource}"
    else:
        raise CallingError("UNSUPPORTED_PROVIDER")
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(10, connect=3), follow_redirects=False
        ) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": "Bearer " + config["access_token"]},
                json=payload,
                params=params,
            )
    except httpx.HTTPError:
        raise CallingError("PROVIDER_UNAVAILABLE", uncertain=method != "GET") from None
    if not 200 <= response.status_code < 300:
        raise CallingError("PROVIDER_REJECTED", uncertain=method != "GET" and response.status_code >= 500)
    try:
        if len(response.content) > 262144:
            raise ValueError()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError()
        if value.get("_errors") or value.get("error"):
            raise CallingError("PROVIDER_REJECTED")
        value = value.get("_data", value)
        if not isinstance(value, dict):
            raise TypeError()
        if value.get("error"):
            raise CallingError("PROVIDER_REJECTED")
        return value
    except (ValueError, TypeError):
        raise CallingError("INVALID_PROVIDER_RESPONSE", uncertain=method != "GET") from None


async def readiness(config, phone_id, *, transport=None):
    try:
        value = await request(config, phone_id, "settings", transport=transport)
        calling = value.get("calling", {})
        if calling.get("status") != "ENABLED":
            return {
                "ready": False,
                "code": "calling_disabled",
                "message": "Enable WhatsApp calling for this business number in Meta or Chakra.",
                "provider_status": calling.get("status", "UNKNOWN"),
            }
        if calling.get("sip", {}).get("status") == "ENABLED":
            return {
                "ready": False,
                "code": "sip_routing",
                "message": "This number uses SIP routing. Configure the platform's WebRTC route before activation.",
            }
        return {"ready": True, "code": "ready", "message": "WhatsApp calling is enabled for this number."}
    except (CallingError, AttributeError, TypeError):
        return {
            "ready": False,
            "code": "provider_unavailable",
            "message": "Unable to verify WhatsApp calling. Reconnect or check your account's calling eligibility.",
        }


async def permission(config, phone_id, recipient, *, transport=None):
    if not re.fullmatch(r"\+[1-9]\d{7,14}", recipient):
        raise CallingError("INVALID_RECIPIENT")
    value = await request(
        config, phone_id, "call_permissions", params={"user_wa_id": recipient[1:]}, transport=transport
    )
    grant = value.get("permission", {})
    expires = grant.get("expiration_time")
    valid = grant.get("status") == "permanent" or (
        grant.get("status") == "temporary" and isinstance(expires, (int, float)) and expires > time.time()
    )
    actions = value.get("actions", [])
    can_call = any(
        a.get("action_name") == "start_call" and a.get("can_perform_action") is True
        for a in actions
        if isinstance(a, dict)
    )
    return {
        "can_call": bool(valid and can_call),
        "permission": grant.get("status", "unknown"),
        "expires_at": expires,
    }


async def start_call(config, phone_id, recipient, sdp, request_id):
    if not (await permission(config, phone_id, recipient))["can_call"]:
        raise CallingError("RECIPIENT_PERMISSION_REQUIRED")
    value = await request(
        config,
        phone_id,
        "calls",
        method="POST",
        payload={
            "messaging_product": "whatsapp",
            "action": "connect",
            "to": recipient[1:],
            "session": {"sdp_type": "offer", "sdp": sdp},
            "biz_opaque_callback_data": str(request_id),
        },
    )
    try:
        ident = value["calls"][0]["id"]
        if not isinstance(ident, str) or not ident.startswith("wacid.") or len(ident) > 300:
            raise ValueError()
        return ident
    except (KeyError, IndexError, ValueError, TypeError):
        raise CallingError("CALL_OUTCOME_UNKNOWN", uncertain=True) from None

"""Bounded TURN credentials for internet audio. The shared secret never reaches a browser."""

import base64
import hashlib
import hmac
import json
import os
import time


def servers(subject):
    secret = os.environ.get("AI_VOICE_TURN_SECRET", "")
    urls = json.loads(os.environ.get("AI_VOICE_TURN_URLS", "[]"))
    if not urls:
        return None
    if (
        len(secret) < 32
        or not isinstance(urls, list)
        or any(not isinstance(u, str) or not u.startswith(("turn:", "turns:")) for u in urls)
    ):
        raise ValueError("Invalid TURN configuration")
    username = str(int(time.time()) + 300) + ":" + str(subject)
    credential = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    return [{"urls": urls, "username": username, "credential": credential}]

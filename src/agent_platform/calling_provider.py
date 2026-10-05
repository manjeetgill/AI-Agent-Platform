"""Twilio form-webhook adapter. No credentials or browser URLs are accepted from calls."""

import json
import os
import re
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

from twilio.request_validator import RequestValidator


def settings(agent):
    cfg = json.loads(os.environ.get("AI_CALLING_CONNECTIONS", "{}")).get(agent.get("connection_ref"))
    if not isinstance(cfg, dict) or cfg.get("provider") != "twilio":
        raise ValueError("Provider not configured")
    if cfg.get("app_id") != str(agent["app_id"]) or cfg.get("owner_id") != str(agent["owner_id"]):
        raise ValueError("Connection owner mismatch")
    if not cfg.get("number_verified") or not re.fullmatch(r"\+[1-9]\d{7,14}", cfg.get("number", "")):
        raise ValueError("Authorized number required")
    if (
        not re.fullmatch(r"AC[a-fA-F0-9]{32}", cfg.get("account_sid", ""))
        or len(cfg.get("auth_token", "")) < 20
    ):
        raise ValueError("Provider credentials required")
    if len(cfg.get("handler_secret", "")) < 32:
        raise ValueError("App handler signing secret required")
    for key in ("public_url", "handler_url"):
        u = urlsplit(cfg.get(key, ""))
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Operator-approved HTTPS endpoints required")
        if key == "public_url" and u.path not in ("", "/"):
            raise ValueError("Public URL must be an origin")
    return cfg


def signature(url, params, token):
    return RequestValidator(token).compute_signature(url, params)


def verify(raw, headers, cfg, path):
    if (
        len(raw) > 65536
        or headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded"
    ):
        raise ValueError("Invalid webhook body")
    pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
    params = dict(pairs)
    # Never silently discard duplicate signed fields.
    if len(params) != len(pairs):
        raise ValueError("Duplicate fields")
    if not RequestValidator(cfg["auth_token"]).validate(
        cfg["public_url"].rstrip("/") + path, params, headers.get("x-twilio-signature", "")
    ):
        raise ValueError("Invalid signature")
    if params.get("AccountSid") != cfg["account_sid"] or params.get("To") != cfg["number"]:
        raise ValueError("Wrong provider account or destination")
    if not re.fullmatch(r"CA[a-fA-F0-9]{32}", params.get("CallSid", "")):
        raise ValueError("Invalid call ID")
    return params


def xml_reply(text, language, action=None):
    root = Element("Response")
    parent = root
    if action:
        parent = SubElement(
            root,
            "Gather",
            input="speech dtmf",
            action=action,
            method="POST",
            language=language,
            timeout="5",
            speechTimeout="auto",
            actionOnEmptyResult="true",
        )
    SubElement(parent, "Say", language=language).text = text
    if not action:
        SubElement(root, "Hangup")
    return tostring(root, encoding="unicode")

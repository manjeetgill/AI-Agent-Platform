"""Transport-independent, grounded receptionist rehearsal and FAQ conversation.

Business mutations remain with the signed app handler; this module has no action tools.
"""

import re
from typing import Literal

from pydantic import Field

from . import knowledge
from .tasks import Strict

LANGUAGES = {
    "auto": "the caller's current language, including mixed Hindi and English when used",
    "en": "English",
    "hi": "Hindi in Devanagari, retaining business names and exact facts",
    "hinglish": "natural Hindi mixed with English; write Hindi words in Devanagari for speech pronunciation",
}


class Message(Strict):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=1500)


class History(Strict):
    history: list[Message] = Field(default_factory=list, max_length=8)


class Preview(History):
    text: str = Field(min_length=1, max_length=1500)
    request_key: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")


class Settings(Strict):
    enabled: bool
    language: Literal["auto", "en", "hi", "hinglish"]


def control(text, language):
    """Exact complete utterances only; never swallow additional symptoms or instructions."""
    normalized = re.sub(r"[.!?।]+$", "", text.strip().lower()).strip()
    hindi = language in {"hi", "hinglish"} or (
        language == "auto" and bool(re.search(r"[\u0900-\u097f]|\b(namaste|dhanyavaad)\b", normalized))
    )
    if normalized in {"hi", "hello", "hey", "namaste", "नमस्ते"}:
        return {
            "action": "continue",
            "text": "नमस्ते, मैं AI assistant हूँ। मैं कैसे मदद करूँ?"
            if hindi
            else "Hello, I'm an AI assistant. How can I help?",
        }
    if normalized in {"bye", "goodbye", "end call", "hang up", "कॉल बंद करो", "call band karo"}:
        return {"action": "end", "text": "धन्यवाद। अलविदा।" if hindi else "Thank you. Goodbye."}
    if normalized in {
        "human",
        "human receptionist",
        "talk to a human",
        "speak to a person",
        "मुझे इंसान से बात करनी है",
        "receptionist se baat karni hai",
    }:
        return {
            "action": "handoff",
            "text": "कृपया reception से सीधे संपर्क करें। मैं यह call transfer नहीं कर सकता।"
            if hindi
            else "Please contact reception directly. I cannot transfer this call to a person.",
        }
    return None


async def respond(pool, gateway, agent, user, text, request_key, history):
    history = History(history=history).model_dump()["history"]
    language = agent.get("language", "auto")
    if language not in LANGUAGES:
        raise ValueError("Unsupported conversation language")
    if reply := control(text, language):
        return reply
    if not agent["documents"]:
        return {"action": "handoff", "text": agent["fallback"]}
    result = await knowledge.answer(
        pool,
        gateway,
        agent["app_id"],
        user,
        text,
        request_key,
        documents=agent["documents"],
        conversation=history,
        language=language,
    )
    if not result["claims"] or result["missing"]:
        return {"action": "handoff", "text": agent["fallback"]}
    return {"action": "continue", "text": " ".join(c["text"] for c in result["claims"])[:1200]}

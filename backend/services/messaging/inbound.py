"""Parse inbound Photon webhook payloads.

No Photon webhook schema is documented in this repo, so common key spellings are
accepted at the top level or nested under "data" / "message".
"""

import re
from urllib.parse import urlsplit

ACK_REPLY = "Got it — analyzing. I'll text you back in a moment."
NO_URL_REPLY = "Send me a post URL to check."
MEDIA_ONLY_REPLY = "I can't analyze attachments yet. Send me a post URL to check."

_CONVERSATION_KEYS = ("conversation_id", "conversationId", "chat_id", "chatGuid", "chat_guid")
_SENDER_KEYS = ("sender_handle", "sender", "from", "handle")
_TEXT_KEYS = ("text", "body", "message")
_MEDIA_KEYS = ("media_urls", "attachments", "media")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING = ".,!?;:)]}'\""
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_ID_LENGTH = 256
_MAX_URL_LENGTH = 8192


def _sources(payload: dict) -> list[dict]:
    return [payload] + [payload[key] for key in ("data", "message") if isinstance(payload.get(key), dict)]


def _first_str(sources: list[dict], keys: tuple[str, ...]) -> str | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _media_urls(sources: list[dict]) -> list[str]:
    for source in sources:
        for key in _MEDIA_KEYS:
            items = source.get(key)
            if isinstance(items, list) and items:
                urls = [item.get("url") if isinstance(item, dict) else item for item in items]
                return [url for url in urls if isinstance(url, str) and _URL.fullmatch(url)]
    return []


def parse_inbound(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Photon payload must be a JSON object.")
    sources = _sources(payload)
    conversation_id = _first_str(sources, _CONVERSATION_KEYS)
    if conversation_id is None or len(conversation_id) > _MAX_ID_LENGTH or _CONTROL.search(conversation_id):
        raise ValueError("Photon payload is missing a valid conversation ID.")
    return {
        "conversation_id": conversation_id,
        "sender_handle": _first_str(sources, _SENDER_KEYS),
        "text": _first_str(sources, _TEXT_KEYS) or "",
        "media_urls": _media_urls(sources),
    }


def extract_url(text: str | None) -> str | None:
    """Return the first http(s) URL in text, without trailing punctuation."""
    if not isinstance(text, str):
        return None
    for match in _URL.finditer(text):
        candidate = match.group(0).rstrip(_URL_TRAILING)
        if len(candidate) > _MAX_URL_LENGTH:
            continue
        try:
            if urlsplit(candidate).hostname:
                return candidate
        except ValueError:
            continue
    return None


def canned_reply(message: dict) -> str | None:
    """Reply for a message the pipeline cannot analyze; None when it has a URL."""
    if extract_url(message.get("text")):
        return None
    return MEDIA_ONLY_REPLY if message.get("media_urls") else NO_URL_REPLY


def is_outbound_echo(payload: dict, sender_id: str) -> bool:
    """Ignore our own sent messages so replies cannot trigger reply loops."""
    sources = _sources(payload)
    if any(source.get(key) is True for source in sources for key in ("is_from_me", "isFromMe", "from_me")):
        return True
    direction = _first_str(sources, ("direction",))
    if direction and direction.lower() in {"outbound", "outgoing", "sent"}:
        return True
    return bool(sender_id) and _first_str(sources, _SENDER_KEYS) == sender_id

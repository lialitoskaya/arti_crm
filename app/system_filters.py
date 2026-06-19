from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.connectors.base import UnifiedChat, UnifiedMessage

# Служебные отправители маркетплейсов, чаты с которыми не должны попадать в CRM.
# notificationuser можно отсеивать сразу по данным из списка чатов, без загрузки
# истории сообщений. chatbot определяется по первому сообщению в истории чата.
BLOCKED_CHAT_SENDERS = {"notificationuser"}
SUPPORT_FIRST_MESSAGE_SENDERS = {"chatbot"}
SYSTEM_SENDER_NAMES = BLOCKED_CHAT_SENDERS | SUPPORT_FIRST_MESSAGE_SENDERS

SENDER_KEY_PARTS = (
    "author",
    "sender",
    "from",
    "user",
    "username",
    "user_name",
    "userName",
    "name",
    "nick",
    "nickname",
    "login",
)

ID_KEY_PARTS = (
    "id",
    "uid",
    "user_id",
    "userId",
    "client_id",
    "clientId",
)


def normalize_sender(value: Any) -> str:
    """Normalize marketplace sender names for stable exact comparisons."""
    return "".join(ch for ch in str(value or "").strip().casefold() if ch.isalnum())


def _candidate_values(value: Any, *, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            key_norm = key_text.casefold()
            should_read_value = any(part.casefold() in key_norm for part in SENDER_KEY_PARTS + ID_KEY_PARTS)
            if should_read_value and not isinstance(item, (dict, list, tuple, set)):
                yield str(item)
            if isinstance(item, (dict, list, tuple, set)):
                yield from _candidate_values(item, depth=depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _candidate_values(item, depth=depth + 1)


def sender_candidates_from_raw(raw: dict[str, Any] | None) -> set[str]:
    return {normalize_sender(item) for item in _candidate_values(raw or {}) if normalize_sender(item)}


def message_sender_candidates(message: UnifiedMessage) -> set[str]:
    values = {
        normalize_sender(message.author),
        normalize_sender(message.raw.get("author") if isinstance(message.raw, dict) else ""),
        normalize_sender(message.raw.get("sender") if isinstance(message.raw, dict) else ""),
        normalize_sender(message.raw.get("user") if isinstance(message.raw, dict) else ""),
    }
    values.update(sender_candidates_from_raw(message.raw))
    return {value for value in values if value}


def chat_sender_candidates(chat: UnifiedChat) -> set[str]:
    values = {
        normalize_sender(chat.customer_name),
        normalize_sender(chat.customer_public_id),
        normalize_sender(chat.metadata.get("author") if isinstance(chat.metadata, dict) else ""),
        normalize_sender(chat.metadata.get("sender") if isinstance(chat.metadata, dict) else ""),
        normalize_sender(chat.metadata.get("user") if isinstance(chat.metadata, dict) else ""),
    }
    values.update(sender_candidates_from_raw(chat.metadata))
    return {value for value in values if value}


def is_notification_user_chat(chat: UnifiedChat) -> bool:
    blocked = {normalize_sender(name) for name in BLOCKED_CHAT_SENDERS}
    return bool(chat_sender_candidates(chat) & blocked)


def is_support_chat_by_first_message(messages: list[UnifiedMessage]) -> bool:
    """Return True when the first/oldest message was sent by marketplace support bot."""
    if not messages:
        return False

    # Если у всех сообщений есть created_at, используем самое раннее время.
    # Если время отсутствует хотя бы у части сообщений, сохраняем порядок,
    # который вернул коннектор, чтобы случайно не переставить историю по id.
    if all(message.created_at for message in messages):
        first = min(messages, key=lambda m: str(m.created_at))
    else:
        first = messages[0]

    blocked = {normalize_sender(name) for name in SUPPORT_FIRST_MESSAGE_SENDERS}
    return bool(message_sender_candidates(first) & blocked)


def system_chat_reason(chat: UnifiedChat, messages: list[UnifiedMessage] | None = None) -> str | None:
    if is_notification_user_chat(chat):
        return "notificationuser"
    if messages is not None and is_support_chat_by_first_message(messages):
        return "first_message_chatbot"
    return None

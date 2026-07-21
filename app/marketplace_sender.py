from __future__ import annotations

import re
from typing import Any


_SENDER_NAME_KEYS = {
    "name", "login", "username", "user_name", "display_name", "displayname",
    "nickname", "author_name", "authorname", "sender_name", "sendername",
    "from_name", "fromname", "system_name", "systemname",
}
_SENDER_CONTAINER_KEYS = {"user", "author", "sender", "from", "participant", "profile"}


def normalize_system_sender(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def system_sender_matches(value: Any, markers: tuple[str, ...]) -> bool:
    normalized = normalize_system_sender(value)
    if not normalized:
        return False
    normalized_markers = {normalize_system_sender(marker) for marker in markers if marker}
    return normalized in normalized_markers


def extract_sender_designations(value: Any, depth: int = 0) -> list[str]:
    if depth > 4 or value in (None, ""):
        return []
    values: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_l = str(key).lower()
            if key_l in _SENDER_NAME_KEYS and isinstance(nested, (str, int, float)):
                values.append(str(nested))
            elif key_l in _SENDER_CONTAINER_KEYS:
                if isinstance(nested, (str, int, float)):
                    values.append(str(nested))
                elif isinstance(nested, dict):
                    values.extend(extract_sender_designations(nested, depth + 1))
    elif isinstance(value, list):
        for item in value[:20]:
            values.extend(extract_sender_designations(item, depth + 1))
    return values

"""Section permissions shared by authentication, API gates and notification delivery."""
from __future__ import annotations

import json
import re
from typing import Any

SECTIONS = {
    "chats": "Чаты", "tasks": "Задачи", "reviews": "Отзывы",
    "questions": "Вопросы", "knowledge": "База знаний", "analytics": "Аналитика",
    "supplyPlanning": "Планирование поставок", "ozonFbo": "Поставки FBO",
    "techSettings": "Технические настройки",
}
LEVELS = {"none": 0, "read": 1, "write": 2}
SHARED_SECTION_ACCESS = {
    "@work": ("chats", "tasks"),
    "@chatSettings": ("chats", "techSettings"),
    "@taskTypes": ("chats", "tasks", "techSettings"),
}


def effective_permissions(role: str, stored: str | dict | None) -> dict[str, str]:
    if role == "admin":
        return dict.fromkeys(SECTIONS, "write")
    if stored is None:
        # Preserve the legacy read access; do not silently grant new write access.
        return {key: "none" if key in {"supplyPlanning", "ozonFbo", "techSettings"} else "read"
                for key in SECTIONS}
    try:
        values = json.loads(stored) if isinstance(stored, str) else stored
    except (ValueError, TypeError):
        values = {}
    if not isinstance(values, dict):
        values = {}
    permissions = dict.fromkeys(SECTIONS, "none")
    for key in SECTIONS:
        value = values.get(key)
        if isinstance(value, str) and value in LEVELS:
            permissions[key] = value
    return permissions


def public_user(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    user = dict(row)
    user.pop("password_hash", None)
    user["permissions"] = effective_permissions(user.get("role", ""), user.pop("section_permissions", None))
    return user


def can(user: dict[str, Any] | None, section: str, level: str = "read") -> bool:
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    permissions = user.get("permissions")
    if permissions is None:
        permissions = effective_permissions(user.get("role", ""), user.get("section_permissions"))
    return LEVELS.get(permissions.get(section), 0) >= LEVELS[level]


def route_requirements(method: str, path: str) -> list[tuple[str, str]] | None:
    """None means admin-only, [] means authenticated self-service/common metadata.

    Paths outside the declared section families require an administrator. Additional object-dependent gates
    belong in the handler, after reading the object and before changing/returning it.
    """
    path = path.rstrip("/") or "/"
    method = method.upper()
    read = method in {"GET", "HEAD"}
    level = "read" if read else "write"
    if (method, path) in {
        ("GET", "/api/auth/me"), ("GET", "/api/security/csrf"),
        ("POST", "/api/auth/logout"), ("PATCH", "/api/auth/profile"),
        ("GET", "/api/permissions"), ("GET", "/api/stats"),
        ("GET", "/api/assets/entity-image"),
        ("GET", "/api/notifications"), ("POST", "/api/notifications/read-all"),
        ("GET", "/api/push/public-key"), ("POST", "/api/push/subscribe"),
        ("POST", "/api/push/unsubscribe"),
    } or (method == "POST" and re.fullmatch(r"/api/notifications/\d+/read", path)):
        return []
    if path == "/api/users/assignees" and read:
        return [("@work", "read")]
    if path.startswith(("/api/users", "/api/admin/", "/api/debug/", "/api/sync/")):
        return None
    # A URL-only image proxy cannot establish ownership of a private attachment.
    if path == "/api/assets/image":
        return None
    if path.startswith("/api/analytics/"):
        if not read:
            return None
        return [("analytics", "read")] + ([("chats", "read")] if path.endswith("/drilldown") else [])
    if path in {"/api/chat-settings", "/api/reply-templates"} and read:
        return [("@chatSettings", "read")]
    if path == "/api/task-types" and read:
        return [("@taskTypes", "read")]
    if path.startswith(("/api/chat-settings/", "/api/task-types", "/api/reply-templates")):
        return [("techSettings", level)]
    if path.startswith("/api/ozon-fbo/"):
        if path.endswith(("/settings", "/api-log", "/check-connection")):
            return None
        return [("ozonFbo", level)]
    if path.startswith("/api/supply-planning/"):
        return [("supplyPlanning", level)]
    if path.startswith("/api/chat-uploads/"):
        return [("chats", "read")] if read else None
    for section in ("chats", "tasks", "reviews", "questions", "knowledge"):
        if path == f"/api/{section}" or path.startswith(f"/api/{section}/"):
            if section == "chats" and re.fullmatch(r"/api/chats/\d+/(read-state|pin-state)", path):
                return [("chats", "read")]
            requirements = [(section, level)]
            if section == "reviews" and path.endswith("/start-chat"):
                requirements.append(("chats", "write"))
            return requirements
    return None


def route_allowed(user: dict[str, Any], method: str, path: str) -> bool:
    return allows_requirements(user, route_requirements(method, path))


def allows_requirements(user: dict[str, Any], requirements: list[tuple[str, str]] | None) -> bool:
    if user.get("role") == "admin":
        return True
    if requirements is None:
        return False
    for section, level in requirements:
        alternatives = SHARED_SECTION_ACCESS.get(section, (section,))
        if not any(can(user, key, level) for key in alternatives):
            return False
    return True


def notification_allowed(user: dict[str, Any], item: dict[str, Any]) -> bool:
    if not user.get("is_active"):
        return False
    if user.get("role") == "admin":
        return True
    entity = item.get("entity_type")
    kind = str(item.get("type") or "")
    required = set()
    if item.get("task_id") or entity == "task" or "task" in kind:
        required.add("tasks")
    if item.get("chat_id"):
        required.add("chats")
    if entity == "question" or "question" in kind:
        required.add("questions")
    if entity == "review" or "review" in kind:
        required.add("reviews")
    if entity == "chat" or "message" in kind:
        required.add("chats")
    return bool(required) and all(can(user, section) for section in required)

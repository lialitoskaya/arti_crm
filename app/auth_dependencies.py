from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, Request


_UNSAFE_API_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_VIEWER_SELF_SERVICE_ROUTES = frozenset(
    {
        ("POST", "/api/auth/logout"),
        ("PATCH", "/api/auth/profile"),
        ("POST", "/api/push/subscribe"),
        ("POST", "/api/push/unsubscribe"),
        ("POST", "/api/notifications/read-all"),
    }
)
_SYSTEM_ROUTE_EXCEPTIONS = frozenset(
    {
        ("GET", "/api/background/tick"),
        ("POST", "/api/background/tick"),
        ("POST", "/api/webhooks/yandex"),
    }
)
_ADMIN_GET_ROUTES = frozenset(
    {
        "/api/users",
        "/api/supply-planning/status",
        "/api/supply-planning/sync",
    }
)
_ADMIN_GET_PREFIXES = ("/api/debug/", "/api/chat-uploads/")
_NOTIFICATION_READ_ROUTE_RE = re.compile(r"^/api/notifications/[^/]+/read$")


def current_user(request: Request, *, auth_disabled: bool = False) -> dict[str, Any]:
    if auth_disabled:
        return {"id": 0, "username": "local", "display_name": "Local", "role": "admin", "is_active": True}
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def require_admin(request: Request, *, auth_disabled: bool = False) -> dict[str, Any]:
    if auth_disabled:
        return current_user(request, auth_disabled=True)
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    return user


def route_access_class(method: str, path: str) -> str:
    normalized_method = str(method or "GET").upper()
    normalized_path = str(path or "/").rstrip("/") or "/"
    route = (normalized_method, normalized_path)

    if normalized_path in {"/", "/health"} or normalized_path.startswith("/static/"):
        return "public"
    if route == ("POST", "/api/auth/login"):
        return "public"
    if route == ("GET", "/api/auth/me"):
        return "session_self_check"
    if route in _SYSTEM_ROUTE_EXCEPTIONS:
        return "token_only" if normalized_path == "/api/background/tick" else "webhook_unchanged"
    if route in _VIEWER_SELF_SERVICE_ROUTES or (
        normalized_method == "POST" and _NOTIFICATION_READ_ROUTE_RE.fullmatch(normalized_path)
    ):
        return "viewer_self_service"
    if normalized_method == "GET" and (
        normalized_path in _ADMIN_GET_ROUTES
        or any(normalized_path.startswith(prefix) for prefix in _ADMIN_GET_PREFIXES)
    ):
        return "admin_only"
    if normalized_path.startswith("/api/") and normalized_method in _UNSAFE_API_METHODS:
        return "admin_only"
    if normalized_path.startswith("/api/"):
        return "authenticated_read"
    return "public"


def route_requires_admin(method: str, path: str) -> bool:
    return route_access_class(method, path) == "admin_only"

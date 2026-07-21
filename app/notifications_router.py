from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request


CurrentUserDependency = Callable[[Request], dict[str, Any]]


def create_notifications_router(
    repo: Any,
    current_user_dependency: CurrentUserDependency,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/notifications")
    def api_notifications(request: Request, limit: int = 30, unread_only: bool = False) -> dict[str, Any]:
        user = current_user_dependency(request)
        return repo.list_notifications(int(user["id"]), limit=limit, unread_only=unread_only)

    @router.post("/api/notifications/{notification_id}/read")
    def api_mark_notification_read(notification_id: int, request: Request) -> dict[str, Any]:
        user = current_user_dependency(request)
        ok = repo.mark_notification_read(notification_id, int(user["id"]))
        return {"ok": ok, "unread_count": repo.list_notifications(int(user["id"]), limit=1)["unread_count"]}

    @router.post("/api/notifications/read-all")
    def api_mark_all_notifications_read(request: Request) -> dict[str, Any]:
        user = current_user_dependency(request)
        count = repo.mark_all_notifications_read(int(user["id"]))
        return {"ok": True, "marked": count, "unread_count": 0}

    return router

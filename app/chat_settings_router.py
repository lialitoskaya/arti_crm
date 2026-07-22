from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ChatFunnelCreate, ChatFunnelUpdate, ChatStatusCreate, ChatStatusUpdate


RequireAdminDependency = Callable[[Request], dict[str, Any]]


def create_chat_settings_router(
    repo: Any,
    require_admin_dependency: RequireAdminDependency,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/chat-settings")
    def get_chat_settings() -> dict[str, Any]:
        return repo.get_chat_settings()

    @router.post("/api/chat-settings/funnels")
    def create_chat_funnel(payload: ChatFunnelCreate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        try:
            return repo.create_chat_funnel(payload.title, payload.sort_order)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/api/chat-settings/funnels/{funnel_id}")
    def update_chat_funnel(funnel_id: int, payload: ChatFunnelUpdate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        try:
            funnel = repo.update_chat_funnel(funnel_id, payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not funnel:
            raise HTTPException(status_code=404, detail="Funnel not found")
        return funnel

    @router.delete("/api/chat-settings/funnels/{funnel_id}")
    def delete_chat_funnel(funnel_id: int, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        try:
            ok = repo.delete_chat_funnel(funnel_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail="Funnel not found")
        return {"ok": True}

    @router.post("/api/chat-settings/statuses")
    def create_chat_status(payload: ChatStatusCreate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        try:
            return repo.create_chat_status(payload.title, payload.key, payload.funnel_id, payload.color, payload.sort_order)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/api/chat-settings/statuses/{status_id}")
    def update_chat_status(status_id: int, payload: ChatStatusUpdate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        status = repo.update_chat_status(status_id, payload.model_dump(exclude_unset=True))
        if not status:
            raise HTTPException(status_code=404, detail="Status not found")
        return status

    @router.delete("/api/chat-settings/statuses/{status_id}")
    def delete_chat_status(status_id: int, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        ok = repo.delete_chat_status(status_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Status not found")
        return {"ok": True}

    return router

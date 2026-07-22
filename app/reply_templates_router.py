from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ReplyTemplateCreate


CurrentUserDependency = Callable[[Request], dict[str, Any]]


def create_reply_templates_router(
    repo: Any,
    current_user_dependency: CurrentUserDependency,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/reply-templates")
    def api_list_reply_templates(request: Request, q: str | None = None) -> list[dict[str, Any]]:
        current_user_dependency(request)
        return repo.list_reply_templates(q=q)

    @router.post("/api/reply-templates")
    def api_create_reply_template(payload: ReplyTemplateCreate, request: Request) -> dict[str, Any]:
        user = current_user_dependency(request)
        try:
            return repo.create_reply_template(
                title=payload.title,
                content=payload.content,
                sort_order=payload.sort_order,
                user_id=int(user["id"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return router

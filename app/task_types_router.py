from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import TaskTypeCreate, TaskTypeUpdate


CurrentUserDependency = Callable[[Request], dict[str, Any]]


def create_task_types_router(
    repo: Any,
    current_user_dependency: CurrentUserDependency,
    require_admin_dependency: CurrentUserDependency,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/task-types")
    def api_list_task_types(request: Request, include_inactive: bool = False) -> list[dict[str, Any]]:
        current_user_dependency(request)
        return repo.list_task_types(include_inactive=include_inactive)

    @router.post("/api/task-types")
    def api_create_task_type(payload: TaskTypeCreate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        try:
            return repo.create_task_type(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.patch("/api/task-types/{type_id}")
    def api_update_task_type(type_id: int, payload: TaskTypeUpdate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        task_type = repo.update_task_type(type_id, payload)
        if not task_type:
            raise HTTPException(status_code=404, detail="Task type not found")
        return task_type

    @router.delete("/api/task-types/{type_id}")
    def api_delete_task_type(type_id: int, request: Request) -> dict[str, bool]:
        require_admin_dependency(request)
        ok = repo.delete_task_type(type_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Task type not found")
        return {"ok": True}

    return router

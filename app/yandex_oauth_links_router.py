"""Administrator endpoints for managing Yandex login rules."""
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import yandex_oauth_links_repository as links
from app.schemas import YandexOAuthManagedLinkCreate, YandexOAuthManagedLinkUpdate


def create_yandex_oauth_links_router(
    require_admin: Callable[[Request], dict[str, Any]],
    list_users: Callable[[], list[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/yandex-oauth-links")
    def api_list_yandex_oauth_managed_links(request: Request) -> dict[str, Any]:
        require_admin(request)
        users = [
            {
                "id": user["id"],
                "username": user["username"],
                "display_name": user.get("display_name"),
                "is_active": bool(user.get("is_active")),
            }
            for user in list_users()
        ]
        return {"links": links.list_yandex_oauth_managed_links(), "users": users}

    @router.post("/api/admin/yandex-oauth-links")
    def api_create_yandex_oauth_managed_link(
        payload: YandexOAuthManagedLinkCreate,
        request: Request,
    ) -> dict[str, Any]:
        require_admin(request)
        try:
            return links.create_yandex_oauth_managed_link(
                identifier_type=payload.identifier_type,
                identifier=payload.identifier,
                crm_user_id=payload.crm_user_id,
            )
        except links.YandexOAuthManagedLinkUserNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Сотрудник не найден") from exc
        except links.YandexOAuthManagedLinkConflictError as exc:
            raise HTTPException(status_code=409, detail="Такой логин или email уже добавлен") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректный логин или email") from exc

    @router.patch("/api/admin/yandex-oauth-links/{link_id}")
    def api_update_yandex_oauth_managed_link(
        link_id: int,
        payload: YandexOAuthManagedLinkUpdate,
        request: Request,
    ) -> dict[str, Any]:
        require_admin(request)
        updated = links.update_yandex_oauth_managed_link(link_id, is_active=payload.is_active)
        if not updated:
            raise HTTPException(status_code=404, detail="Связь не найдена")
        return updated

    @router.delete("/api/admin/yandex-oauth-links/{link_id}")
    def api_delete_yandex_oauth_managed_link(link_id: int, request: Request) -> dict[str, bool]:
        require_admin(request)
        if not links.delete_yandex_oauth_managed_link(link_id):
            raise HTTPException(status_code=404, detail="Связь не найдена")
        return {"ok": True}

    return router

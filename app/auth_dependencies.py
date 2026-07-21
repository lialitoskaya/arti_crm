from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def current_user(request: Request, *, auth_disabled: bool = False) -> dict[str, Any]:
    if auth_disabled:
        return {"id": 0, "username": "local", "display_name": "Local", "role": "admin", "is_active": True}
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    return user

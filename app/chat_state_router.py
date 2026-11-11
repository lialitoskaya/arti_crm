"""Personal chat marks, scoped to the current CRM user."""
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import chat_state_repository
from app.schemas import ChatPinStateUpdate, ChatReadStateUpdate


def create_chat_state_router(current_user: Callable[[Request], dict[str, Any]]) -> APIRouter:
    router = APIRouter()

    @router.patch("/api/chats/{chat_id}/read-state")
    def update_chat_read_state(
        chat_id: int,
        payload: ChatReadStateUpdate,
        request: Request,
    ) -> dict[str, Any]:
        user = current_user(request)
        state = chat_state_repository.set_chat_read_state(
            chat_id,
            int(user["id"]),
            is_unread=payload.is_unread,
        )
        if not state:
            raise HTTPException(status_code=404, detail="Chat not found")
        return state

    @router.patch("/api/chats/{chat_id}/pin-state")
    def update_chat_pin_state(
        chat_id: int,
        payload: ChatPinStateUpdate,
        request: Request,
    ) -> dict[str, Any]:
        user = current_user(request)
        state = chat_state_repository.set_chat_pin_state(
            chat_id,
            int(user["id"]),
            is_pinned=payload.is_pinned,
        )
        if not state:
            raise HTTPException(status_code=404, detail="Chat not found")
        return state

    return router

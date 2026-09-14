from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from app import repository as repo
from app.connectors.base import MarketplaceConnector
from app.message_send_models import (
    MESSAGE_SEND_ATTEMPT_TIMEOUT_SECONDS,
    MarketplaceSendError,
    MarketplaceSendOutcome,
)
from app.message_send_operations import (
    MessageSendOperationConflict,
    claim_next_operation,
    complete_operation_accepted,
    complete_operation_error,
    get_operation,
    get_operation_by_client_id,
    list_chat_operations,
    mark_stale_sending_uncertain,
    register_operation,
)


logger = logging.getLogger(__name__)


class MessageSendService:
    """One canonical text-command dispatcher for routes, startup and workers."""

    def __init__(self, connectors: Mapping[str, MarketplaceConnector]) -> None:
        self._connectors = connectors

    async def register_and_dispatch(
        self,
        *,
        chat_id: int,
        client_operation_id: str,
        text: str,
        intent_origin: str,
        author_user_id: int | None,
        author_label: str,
    ) -> tuple[dict[str, Any], bool]:
        chat = repo.get_chat(chat_id)
        if not chat:
            raise LookupError("Chat not found")
        connector = self._connector_for_chat(chat)
        if connector is None:
            raise ValueError("Marketplace connector is not configured")
        normalized_text = connector.normalize_text_command(text)
        operation, deduplicated = register_operation(
            chat_id=chat_id,
            client_operation_id=client_operation_id,
            text=normalized_text,
            intent_origin=intent_origin,
            author_user_id=author_user_id,
            author_label=author_label,
        )
        if operation.get("status") in {"pending", "retry_wait"}:
            operation = await self.dispatch_operation(int(operation["id"])) or operation
        return operation, deduplicated

    def _connector_for_chat(self, chat: dict[str, Any]) -> MarketplaceConnector | None:
        connector_key = (
            "mock"
            if str((chat.get("metadata") or {}).get("source") or "") == "mock"
            else str(chat.get("marketplace") or "")
        )
        return self._connectors.get(connector_key)

    async def dispatch_operation(self, operation_id: int) -> dict[str, Any] | None:
        claimed = claim_next_operation(operation_id=int(operation_id))
        if not claimed:
            return get_operation(operation_id)
        return await self._dispatch_claimed(claimed)

    async def _dispatch_claimed(self, operation: dict[str, Any]) -> dict[str, Any]:
        operation_id = int(operation["id"])
        claim_token = str(operation["claim_token"])

        chat = repo.get_chat(int(operation["chat_id"]))
        if not chat:
            error = MarketplaceSendError(
                category="invalid_target",
                safe_summary="Chat no longer exists",
                side_effect_possible=False,
            )
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=error,
            )
            return updated or operation
        if (
            str(chat.get("marketplace")) != str(operation["marketplace"])
            or str(chat.get("external_chat_id")) != str(operation["external_chat_id"])
        ):
            error = MarketplaceSendError(
                category="invalid_target",
                safe_summary="Stored send target no longer matches the chat",
                side_effect_possible=False,
            )
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=error,
            )
            return updated or operation

        connector = self._connector_for_chat(chat)
        if connector is None:
            error = MarketplaceSendError(
                category="configuration",
                safe_summary="Marketplace connector is not configured",
                side_effect_possible=False,
            )
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=error,
            )
            return updated or operation

        if str(operation["marketplace"]) == "wildberries" and hasattr(
            connector, "set_reply_sign_from_metadata"
        ):
            connector.set_reply_sign_from_metadata(
                str(operation["external_chat_id"]),
                chat.get("metadata") or {},
            )

        payload = operation.get("payload") or {}
        text = str(payload.get("text") or "")
        try:
            async with asyncio.timeout(MESSAGE_SEND_ATTEMPT_TIMEOUT_SECONDS):
                outcome = await connector.send_message(
                    str(operation["external_chat_id"]),
                    text,
                )
        except MarketplaceSendError as exc:
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=exc,
            )
            return updated or get_operation(operation_id) or operation
        except TimeoutError:
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=MarketplaceSendError(
                    category="ambiguous_timeout",
                    safe_summary="Marketplace send timed out; provider outcome is unknown",
                    side_effect_possible=True,
                ),
            )
            return updated or get_operation(operation_id) or operation
        except Exception:
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=MarketplaceSendError(
                    category="ambiguous_transport",
                    safe_summary="Marketplace send failed; provider outcome is unknown",
                    side_effect_possible=True,
                ),
            )
            return updated or get_operation(operation_id) or operation

        if not isinstance(outcome, MarketplaceSendOutcome):
            updated, _ = complete_operation_error(
                operation_id=operation_id,
                claim_token=claim_token,
                error=MarketplaceSendError(
                    category="connector_contract",
                    safe_summary="Marketplace connector returned an invalid send outcome",
                    side_effect_possible=True,
                ),
            )
            return updated or get_operation(operation_id) or operation

        updated, _ = complete_operation_accepted(
            operation_id=operation_id,
            claim_token=claim_token,
            provider_external_message_id=outcome.provider_external_message_id,
        )
        if updated and updated.get("status") in {"accepted", "confirmed"}:
            author_user_id = int(updated.get("author_user_id") or 0)
            if author_user_id:
                repo.assign_chat_to_user_if_unassigned(
                    chat_id=int(updated["chat_id"]),
                    user_id=author_user_id,
                    reason="first_crm_reply",
                )
            if updated.get("status") == "accepted":
                await self._reconcile_after_accept(connector, updated)
        return get_operation(operation_id) or updated or operation

    async def _reconcile_after_accept(
        self,
        connector: MarketplaceConnector,
        operation: dict[str, Any],
    ) -> None:
        try:
            messages = await connector.get_messages(str(operation["external_chat_id"]))
        except Exception:
            # Send acceptance is already durable. A failed history refresh must not
            # turn it into an ambiguous send or trigger another provider attempt.
            logger.info(
                "Marketplace history reconciliation deferred",
                extra={"operation_id": int(operation["id"])},
            )
            return
        for message in messages:
            if str(message.external_chat_id) != str(operation["external_chat_id"]):
                continue
            repo.add_message(
                chat_id=int(operation["chat_id"]),
                direction=message.direction,
                text=message.text,
                author=message.author,
                external_message_id=message.external_message_id,
                raw=message.raw,
                created_at=message.created_at,
            )

    async def drain_once(self, *, limit: int = 20) -> dict[str, int]:
        stale = mark_stale_sending_uncertain()
        processed = 0
        for _ in range(max(1, min(200, int(limit)))):
            claimed = claim_next_operation()
            if not claimed:
                break
            processed += 1
            try:
                await self._dispatch_claimed(claimed)
            except Exception:
                # _dispatch_claimed persists each operation failure. This guard keeps
                # one unexpected record from stopping the bounded drain loop.
                logger.exception(
                    "Unexpected message send dispatcher failure",
                    extra={"operation_id": int(claimed["id"])},
                )
        return {"processed": processed, "stale_uncertain": stale}

    def list_chat_operations(self, chat_id: int) -> list[dict[str, Any]]:
        return list_chat_operations(chat_id)

    def get_chat_operation(
        self,
        chat_id: int,
        client_operation_id: str,
    ) -> dict[str, Any] | None:
        return get_operation_by_client_id(chat_id, client_operation_id)


def public_operation(operation: dict[str, Any], *, deduplicated: bool = False) -> dict[str, Any]:
    return {
        "id": int(operation["id"]),
        "chat_id": int(operation["chat_id"]),
        "client_operation_id": str(operation["client_operation_id"]),
        "command_kind": str(operation["command_kind"]),
        "intent_origin": str(operation["intent_origin"]),
        "status": str(operation["status"]),
        "attempt_count": int(operation.get("attempt_count") or 0),
        "text": str((operation.get("payload") or {}).get("text") or ""),
        "author_label": str(operation.get("author_label") or ""),
        "requested_at": operation.get("requested_at"),
        "next_attempt_at": operation.get("next_attempt_at"),
        "canonical_message_id": operation.get("canonical_message_id"),
        "provider_external_message_id": operation.get("provider_external_message_id"),
        "error": (
            {
                "category": operation.get("error_category"),
                "http_status": operation.get("error_http_status"),
                "provider_code": operation.get("error_provider_code"),
                "correlation_id": operation.get("error_correlation_id"),
                "summary": operation.get("error_summary"),
            }
            if operation.get("error_category")
            else None
        ),
        "deduplicated": bool(deduplicated),
    }


__all__ = [
    "MessageSendOperationConflict",
    "MessageSendService",
    "public_operation",
]

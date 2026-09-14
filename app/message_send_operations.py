from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from app.db import get_connection
from app.message_send_models import (
    MESSAGE_SEND_ECHO_MATCH_EARLY_SECONDS,
    MESSAGE_SEND_ECHO_MATCH_LATE_SECONDS,
    MESSAGE_SEND_ECHO_CONFIRMABLE_STATUSES,
    MESSAGE_SEND_LEASE_SECONDS,
    MESSAGE_SEND_MAX_SAFE_ATTEMPTS,
    MESSAGE_SEND_RECONCILIATION_STATUS_SQL,
    MESSAGE_SEND_SAFE_BACKOFF_SECONDS,
    MarketplaceSendError,
    canonical_message_payload,
)


class MessageSendOperationConflict(RuntimeError):
    pass


def _db_time(conn: sqlite3.Connection, modifier: str | None = None) -> str:
    modifiers = ", ?" if modifier else ""
    params: tuple[Any, ...] = (modifier,) if modifier else ()
    row = conn.execute(
        f"SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now'{modifiers}) AS value",
        params,
    ).fetchone()
    return str(row["value"])


def _operation_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    try:
        payload = json.loads(result.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    result["payload"] = payload if isinstance(payload, dict) else {}
    return result


def _fetch_operation_conn(
    conn: sqlite3.Connection,
    operation_id: int,
) -> dict[str, Any] | None:
    return _operation_dict(
        conn.execute(
            "SELECT * FROM message_send_operations WHERE id=?",
            (int(operation_id),),
        ).fetchone()
    )


def get_operation(operation_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        return _fetch_operation_conn(conn, operation_id)


def get_operation_by_client_id(
    chat_id: int,
    client_operation_id: str,
) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM message_send_operations
            WHERE chat_id=? AND client_operation_id=?
            """,
            (int(chat_id), str(client_operation_id or "").strip()),
        ).fetchone()
        return _operation_dict(row)


def register_operation(
    *,
    chat_id: int,
    client_operation_id: str,
    text: str,
    intent_origin: str,
    author_user_id: int | None,
    author_label: str,
) -> tuple[dict[str, Any], bool]:
    operation_key = str(client_operation_id or "").strip()
    if not operation_key:
        raise ValueError("client_operation_id is required")
    if intent_origin not in {"message", "attachment_caption"}:
        raise ValueError("Unsupported message intent origin")
    normalized_text, payload_json, payload_hash = canonical_message_payload(text)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        chat = conn.execute(
            "SELECT id, marketplace, external_chat_id FROM chats WHERE id=?",
            (int(chat_id),),
        ).fetchone()
        if not chat:
            raise LookupError("Chat not found")
        existing = conn.execute(
            """
            SELECT * FROM message_send_operations
            WHERE chat_id=? AND client_operation_id=?
            """,
            (int(chat_id), operation_key),
        ).fetchone()
        if existing:
            if str(existing["payload_hash"] or "") != payload_hash:
                raise MessageSendOperationConflict(
                    "client_operation_id is already registered with another payload"
                )
            return _operation_dict(existing) or {}, True

        now = _db_time(conn)
        cursor = conn.execute(
            """
            INSERT INTO message_send_operations (
                chat_id, marketplace, external_chat_id, client_operation_id,
                command_kind, intent_origin, payload_json, payload_hash,
                author_user_id, author_label, status, requested_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'chat_text', ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                int(chat_id),
                str(chat["marketplace"]),
                str(chat["external_chat_id"]),
                operation_key,
                intent_origin,
                payload_json,
                payload_hash,
                int(author_user_id) if author_user_id else None,
                str(author_label or "").strip(),
                now,
                now,
            ),
        )
        created = _fetch_operation_conn(conn, int(cursor.lastrowid))
        return created or {}, False


def mark_stale_sending_uncertain() -> int:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = _db_time(conn)
        cursor = conn.execute(
            """
            UPDATE message_send_operations
            SET status='uncertain', error_category='stale_claim',
                error_summary='Send attempt lease expired; provider outcome is unknown',
                next_attempt_at=NULL, updated_at=?
            WHERE status='sending' AND lease_until < ?
            """,
            (now, now),
        )
        return int(cursor.rowcount)


def claim_next_operation(
    *,
    operation_id: int | None = None,
) -> dict[str, Any] | None:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = _db_time(conn)
        params: list[Any] = [now]
        id_clause = ""
        if operation_id is not None:
            id_clause = "AND candidate.id=?"
            params.append(int(operation_id))
        candidate = conn.execute(
            f"""
            SELECT candidate.id
            FROM message_send_operations AS candidate
            WHERE (
                    candidate.status='pending'
                    OR (
                        candidate.status='retry_wait'
                        AND candidate.next_attempt_at <= ?
                    )
                  )
              {id_clause}
              AND NOT EXISTS (
                    SELECT 1
                    FROM message_send_operations AS blocker
                    WHERE blocker.chat_id=candidate.chat_id
                      AND blocker.payload_hash=candidate.payload_hash
                      AND blocker.canonical_message_id IS NULL
                      AND blocker.status IN ({MESSAGE_SEND_RECONCILIATION_STATUS_SQL})
                      AND blocker.id<>candidate.id
                  )
            ORDER BY candidate.requested_at, candidate.id
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if not candidate:
            return None

        token = uuid.uuid4().hex
        lease_until = _db_time(conn, f"+{MESSAGE_SEND_LEASE_SECONDS} seconds")
        cursor = conn.execute(
            """
            UPDATE message_send_operations
            SET status='sending', claim_token=?,
                attempt_count=attempt_count+1, claimed_at=?, last_attempt_at=?,
                lease_until=?, next_attempt_at=NULL, updated_at=?
            WHERE id=?
              AND (
                    status='pending'
                    OR (status='retry_wait' AND next_attempt_at <= ?)
                  )
            """,
            (
                token,
                now,
                now,
                lease_until,
                now,
                int(candidate["id"]),
                now,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return _fetch_operation_conn(conn, int(candidate["id"]))


def complete_operation_accepted(
    *,
    operation_id: int,
    claim_token: str,
    provider_external_message_id: str | None,
) -> tuple[dict[str, Any] | None, bool]:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = _db_time(conn)
        cursor = conn.execute(
            """
            UPDATE message_send_operations
            SET status='accepted', accepted_at=?,
                provider_external_message_id=COALESCE(?, provider_external_message_id),
                error_category=NULL, error_http_status=NULL,
                error_provider_code=NULL, error_correlation_id=NULL,
                error_summary=NULL, updated_at=?
            WHERE id=? AND claim_token=?
              AND (
                    status='sending'
                    OR (status='uncertain' AND error_category='stale_claim')
                  )
            """,
            (
                now,
                str(provider_external_message_id or "").strip() or None,
                now,
                int(operation_id),
                str(claim_token),
            ),
        )
        return _fetch_operation_conn(conn, operation_id), cursor.rowcount == 1


def complete_operation_error(
    *,
    operation_id: int,
    claim_token: str,
    error: MarketplaceSendError,
) -> tuple[dict[str, Any] | None, bool]:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT attempt_count FROM message_send_operations WHERE id=?",
            (int(operation_id),),
        ).fetchone()
        attempt_count = int(current["attempt_count"] or 0) if current else 0
        now = _db_time(conn)
        next_attempt_at: str | None = None
        failed_at: str | None = None
        if error.side_effect_possible:
            target_status = "uncertain"
        elif error.retryable and attempt_count < MESSAGE_SEND_MAX_SAFE_ATTEMPTS:
            target_status = "retry_wait"
            delay_index = max(0, min(attempt_count - 1, len(MESSAGE_SEND_SAFE_BACKOFF_SECONDS) - 1))
            delay = error.retry_after_seconds or MESSAGE_SEND_SAFE_BACKOFF_SECONDS[delay_index]
            next_attempt_at = _db_time(conn, f"+{int(delay)} seconds")
        else:
            target_status = "permanent_failed"
            failed_at = now

        cursor = conn.execute(
            """
            UPDATE message_send_operations
            SET status=?, next_attempt_at=?, failed_at=?,
                error_category=?, error_http_status=?, error_provider_code=?,
                error_correlation_id=?, error_summary=?, updated_at=?
            WHERE id=? AND status='sending' AND claim_token=?
            """,
            (
                target_status,
                next_attempt_at,
                failed_at,
                error.category,
                error.http_status,
                error.provider_code,
                error.correlation_id,
                error.safe_summary,
                now,
                int(operation_id),
                str(claim_token),
            ),
        )
        return _fetch_operation_conn(conn, operation_id), cursor.rowcount == 1


def match_operation_for_echo_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    direction: str,
    text: str,
    provider_external_message_id: str | None,
    created_at: str | None,
) -> dict[str, Any] | None:
    if str(direction or "").strip().lower() != "outbound":
        return None
    external_id = str(provider_external_message_id or "").strip()
    status_sql = ", ".join(f"'{status}'" for status in MESSAGE_SEND_ECHO_CONFIRMABLE_STATUSES)
    if external_id:
        row = conn.execute(
            f"""
            SELECT * FROM message_send_operations
            WHERE chat_id=? AND provider_external_message_id=?
              AND status IN ({status_sql})
            LIMIT 1
            """,
            (int(chat_id), external_id),
        ).fetchone()
        if row:
            return _operation_dict(row)

    try:
        _, _, payload_hash = canonical_message_payload(text)
    except ValueError:
        return None
    echo_created_at = str(created_at or "").strip()
    if not echo_created_at:
        return None
    row = conn.execute(
        f"""
        SELECT * FROM message_send_operations
        WHERE chat_id=? AND payload_hash=? AND canonical_message_id IS NULL
          AND provider_external_message_id IS NULL
          AND last_attempt_at IS NOT NULL
          AND status IN ({status_sql})
          AND julianday(?) BETWEEN
              julianday(last_attempt_at, '-{MESSAGE_SEND_ECHO_MATCH_EARLY_SECONDS} seconds')
              AND julianday(last_attempt_at, '+{MESSAGE_SEND_ECHO_MATCH_LATE_SECONDS} seconds')
        ORDER BY last_attempt_at, id
        LIMIT 1
        """,
        (int(chat_id), payload_hash, echo_created_at),
    ).fetchone()
    return _operation_dict(row)


def confirm_operation_from_echo_conn(
    conn: sqlite3.Connection,
    *,
    operation_id: int,
    canonical_message_id: int,
    provider_external_message_id: str | None,
) -> bool:
    status_sql = ", ".join(f"'{status}'" for status in MESSAGE_SEND_ECHO_CONFIRMABLE_STATUSES)
    now = _db_time(conn)
    cursor = conn.execute(
        f"""
        UPDATE message_send_operations
        SET status='confirmed', canonical_message_id=?,
            provider_external_message_id=COALESCE(?, provider_external_message_id),
            confirmed_at=?, updated_at=?
        WHERE id=? AND status IN ({status_sql})
        """,
        (
            int(canonical_message_id),
            str(provider_external_message_id or "").strip() or None,
            now,
            now,
            int(operation_id),
        ),
    )
    return cursor.rowcount == 1


def list_chat_operations(chat_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM message_send_operations
            WHERE chat_id=?
              AND canonical_message_id IS NULL
              AND status <> 'confirmed'
            ORDER BY id DESC
            """,
            (int(chat_id),),
        ).fetchall()
        return [_operation_dict(row) or {} for row in rows]


def count_operations() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM message_send_operations").fetchone()
        return int(row["count"] or 0)

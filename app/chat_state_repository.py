"""Per-user read and pin state for CRM chats."""
from __future__ import annotations

from typing import Any

from app.db import get_connection


def _chat_read_state_row_conn(conn: Any, chat_id: int, user_id: int) -> Any:
    return conn.execute(
        """
        SELECT
            c.id AS chat_id,
            cus.user_id AS state_user_id,
            cus.last_read_message_id,
            cus.last_read_at,
            COALESCE(cus.is_marked_unread, 0) AS is_marked_unread,
            (
                SELECT MAX(all_messages.id)
                FROM messages all_messages
                WHERE all_messages.chat_id=c.id
            ) AS latest_message_id,
            (
                SELECT MAX(unread_messages.id)
                FROM messages unread_messages
                WHERE unread_messages.chat_id=c.id
                  AND unread_messages.direction='inbound'
                  AND cus.user_id IS NOT NULL
                  AND unread_messages.id > COALESCE(cus.last_read_message_id, 0)
            ) AS unread_message_id
        FROM chats c
        LEFT JOIN chat_user_states cus
          ON cus.chat_id=c.id AND cus.user_id=?
        WHERE c.id=?
        """,
        (int(user_id), int(chat_id)),
    ).fetchone()


def _serialize_chat_read_state(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    marked_unread = bool(row["is_marked_unread"])
    unread_message_id = row["unread_message_id"]
    return {
        "chat_id": int(row["chat_id"]),
        "is_unread": bool(marked_unread or unread_message_id is not None),
        "is_marked_unread": marked_unread,
        "last_read_message_id": (
            int(row["last_read_message_id"])
            if row["last_read_message_id"] is not None
            else None
        ),
        "last_read_at": row["last_read_at"],
        "unread_message_id": (
            int(unread_message_id)
            if unread_message_id is not None
            else None
        ),
    }


def get_chat_read_state(chat_id: int, user_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        return _serialize_chat_read_state(
            _chat_read_state_row_conn(conn, int(chat_id), int(user_id))
        )


def set_chat_read_state(
    chat_id: int,
    user_id: int,
    *,
    is_unread: bool,
) -> dict[str, Any] | None:
    """Set canonical read state for one CRM user without loading chat history."""
    with get_connection() as conn:
        current = _chat_read_state_row_conn(conn, int(chat_id), int(user_id))
        if not current:
            return None

        latest_message_id = current["latest_message_id"]
        if is_unread:
            conn.execute(
                """
                INSERT INTO chat_user_states (
                    user_id, chat_id, last_read_message_id, last_read_at,
                    is_marked_unread, updated_at
                )
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    is_marked_unread=1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE chat_user_states.is_marked_unread<>1
                """,
                (int(user_id), int(chat_id), latest_message_id),
            )
        elif current["state_user_id"] is not None:
            conn.execute(
                """
                UPDATE chat_user_states
                SET last_read_message_id=?,
                    last_read_at=CURRENT_TIMESTAMP,
                    is_marked_unread=0,
                    updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND chat_id=?
                  AND (
                      is_marked_unread<>0
                      OR last_read_message_id IS NOT ?
                  )
                """,
                (
                    latest_message_id,
                    int(user_id),
                    int(chat_id),
                    latest_message_id,
                ),
            )

        return _serialize_chat_read_state(
            _chat_read_state_row_conn(conn, int(chat_id), int(user_id))
        )


def _chat_pin_state_row_conn(conn: Any, chat_id: int, user_id: int) -> Any:
    return conn.execute(
        """
        SELECT
            c.id AS chat_id,
            cus.user_id AS state_user_id,
            COALESCE(cus.is_pinned, 0) AS is_pinned,
            cus.pinned_at AS pinned_at,
            (
                SELECT MAX(all_messages.id)
                FROM messages all_messages
                WHERE all_messages.chat_id=c.id
            ) AS latest_message_id
        FROM chats c
        LEFT JOIN chat_user_states cus
          ON cus.chat_id=c.id AND cus.user_id=?
        WHERE c.id=?
        """,
        (int(user_id), int(chat_id)),
    ).fetchone()


def _serialize_chat_pin_state(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "chat_id": int(row["chat_id"]),
        "is_pinned": bool(row["is_pinned"]),
        "pinned_at": row["pinned_at"],
    }


def get_chat_pin_state(chat_id: int, user_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        return _serialize_chat_pin_state(
            _chat_pin_state_row_conn(conn, int(chat_id), int(user_id))
        )


def set_chat_pin_state(
    chat_id: int,
    user_id: int,
    *,
    is_pinned: bool,
) -> dict[str, Any] | None:
    """Set personal pin state without changing read state or loading history."""
    with get_connection() as conn:
        current = _chat_pin_state_row_conn(conn, int(chat_id), int(user_id))
        if not current:
            return None

        if is_pinned:
            conn.execute(
                """
                INSERT INTO chat_user_states (
                    user_id, chat_id, last_read_message_id, last_read_at,
                    is_marked_unread, is_pinned, pinned_at, updated_at
                )
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    is_pinned=1,
                    pinned_at=CASE
                        WHEN chat_user_states.is_pinned=1 THEN chat_user_states.pinned_at
                        ELSE CURRENT_TIMESTAMP
                    END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE chat_user_states.is_pinned<>1
                """,
                (int(user_id), int(chat_id), current["latest_message_id"]),
            )
        elif current["state_user_id"] is not None:
            conn.execute(
                """
                UPDATE chat_user_states
                SET is_pinned=0,
                    pinned_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE user_id=? AND chat_id=? AND is_pinned<>0
                """,
                (int(user_id), int(chat_id)),
            )

        return _serialize_chat_pin_state(
            _chat_pin_state_row_conn(conn, int(chat_id), int(user_id))
        )

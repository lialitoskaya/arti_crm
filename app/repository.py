from __future__ import annotations

import os

import json
import re
from datetime import datetime, timezone
from typing import Any

from app.db import get_connection
from app.schemas import ChatCreate, ChatUpdate, TaskCreate, TaskUpdate


STATUS_LABELS = {
    "new": "Новый",
    "in_progress": "В работе",
    "waiting_customer": "Ждём клиента",
    "closed": "Закрыт",
}





def _normalize_ozon_question_status(status: Any) -> str:
    """Normalize Ozon question status values returned by different API variants."""
    value = str(status or "").strip()
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def _is_ozon_question_processed_status(status: Any) -> bool:
    token = _normalize_ozon_question_status(status)
    return token in {
        "processed",
        "answered",
        "done",
        "closed",
        "resolved",
        "обработан",
        "обработано",
        "ответ дан",
        "есть ответ",
        "отвечен",
        "отвечено",
    }


def _user_label_from_row(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    return (row.get("display_name") or row.get("username") or "").strip() or None


def _get_user_label(conn, user_id: int | None) -> str | None:
    if not user_id:
        return None
    row = conn.execute("SELECT id, username, display_name FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    return _user_label_from_row(row_to_dict(row)) if row else None

def row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    if "metadata_json" in data:
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    if "raw_json" in data:
        data["raw"] = json.loads(data.pop("raw_json") or "{}")
    return data




def _slugify_chat_status_key(title: str) -> str:
    raw = str(title or "").strip().lower()
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    raw = "".join(translit.get(ch, ch) for ch in raw)
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return raw or "status"


def _unique_status_key(conn, title: str, preferred: str | None = None) -> str:
    base = _slugify_chat_status_key(preferred or title)
    candidate = base
    counter = 2
    while conn.execute("SELECT 1 FROM chat_statuses WHERE key=?", (candidate,)).fetchone():
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _status_map(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*, f.title AS funnel_title
        FROM chat_statuses s
        LEFT JOIN chat_funnels f ON f.id=s.funnel_id
        """
    ).fetchall()
    return {row["key"]: row_to_dict(row) for row in rows}


def _chat_status_blocks_waiting_response(chat: dict[str, Any]) -> bool:
    status = str(chat.get("status") or "").strip().lower()
    label = str(chat.get("status_title") or chat.get("status_label") or STATUS_LABELS.get(status, "") or "").strip().lower()
    normalized_label = label.replace("ё", "е")
    if status in {"closed", "waiting_customer"}:
        return True
    if "закры" in normalized_label:
        return True
    if "ждем клиент" in normalized_label or "ждём клиент" in label:
        return True
    return False


def _closed_status_condition_sql() -> str:
    # Closed/archived workflow statuses must stay out of the active inbox.
    # This also covers user-created statuses with title "Закрыт" and generated
    # keys such as "zakryt", not only the built-in key "closed".
    return (
        "("
        "c.status='closed' OR "
        "lower(c.status) IN ('closed', 'archive', 'archived', 'zakryt', 'zakryto') OR "
        "c.status LIKE '%Закры%' OR c.status LIKE '%закры%' OR "
        "c.status IN ("
        "SELECT key FROM chat_statuses "
        "WHERE key='closed' "
        "OR lower(key) IN ('closed', 'archive', 'archived', 'zakryt', 'zakryto') "
        "OR title LIKE '%Закры%' "
        "OR title LIKE '%закры%'"
        ")"
        ")"
    )


def _is_closed_status_key_conn(conn, status_value: str | None) -> bool:
    raw = str(status_value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in {"closed", "archive", "archived", "zakryt", "zakryto"}:
        return True
    if "закры" in lowered or "архив" in lowered:
        return True
    row = conn.execute(
        """
        SELECT key, title
        FROM chat_statuses
        WHERE key=?
        LIMIT 1
        """,
        (raw,),
    ).fetchone()
    if not row:
        return False
    title = str(row["title"] or "").strip().lower().replace("ё", "е")
    key = str(row["key"] or "").strip().lower()
    return key in {"closed", "archive", "archived", "zakryt", "zakryto"} or "закры" in title or "архив" in title


def _canonical_workflow_status_conn(conn, status_value: str | None) -> str:
    raw = str(status_value or "").strip()
    if _is_closed_status_key_conn(conn, raw):
        return "closed"
    return raw


def _decorate_chat_sla(chat: dict[str, Any]) -> dict[str, Any]:
    """Add simple SLA flags and repair visible last-message fields for UI."""
    actual_text = chat.get("actual_last_message_text")
    actual_at = chat.get("actual_last_message_at")
    if actual_at:
        chat["last_message_at"] = actual_at
    if actual_text is not None:
        chat["last_message_preview"] = _message_preview(actual_text)

    direction = chat.get("last_message_direction")
    waiting = direction == "inbound" and not _chat_status_blocks_waiting_response(chat)
    chat["sla_waiting_response"] = waiting
    chat["sla_waiting_since_at"] = chat.get("last_message_at") if waiting else None
    chat["sla_label"] = "ждёт ответа" if waiting else None
    chat["status_label"] = chat.get("status_title") or STATUS_LABELS.get(chat.get("status"), chat.get("status"))
    if chat.get("status_color"):
        chat["status_color"] = chat.get("status_color")
    if chat.get("funnel_id"):
        chat["funnel_id"] = chat.get("funnel_id")
    if chat.get("funnel_title"):
        chat["funnel_title"] = chat.get("funnel_title")
    return chat


def _parse_message_timestamp(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    text = str(value).strip()
    if not text or text.startswith('0000-') or text.startswith('0001-'):
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace(' ', 'T'))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def find_recent_matching_outbound_message(
    chat_id: int,
    text: str | None,
    created_at: str | None = None,
    *,
    window_seconds: int = 600,
) -> dict[str, Any] | None:
    """Find a recently saved CRM outbound message matching a marketplace echo.

    WB chat-list `lastMessage` can contain only text/time without reliable sender
    direction. If WB later echoes our own reply without a sender field, matching it
    to the local outbound message prevents the chat from being marked as
    "ждёт ответа".
    """
    needle = (text or '').strip()
    if not needle or needle == '[сообщение без текста / вложение]':
        return None
    target_ts = _parse_message_timestamp(created_at)
    if target_ts is None:
        return None
    safe_window = max(30, min(int(window_seconds or 600), 86400))

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, external_message_id, direction, author, text, created_at
            FROM messages
            WHERE chat_id=?
              AND direction='outbound'
              AND TRIM(text)=TRIM(?)
            ORDER BY id DESC
            LIMIT 20
            """,
            (int(chat_id), needle),
        ).fetchall()

    for row in rows:
        row_d = row_to_dict(row)
        local_ts = _parse_message_timestamp(row_d.get('created_at'))
        if not local_ts:
            continue
        if abs((target_ts - local_ts).total_seconds()) <= safe_window:
            return row_d
    return None




def _is_provisional_outbound_external_id(value: Any) -> bool:
    """Return True for local send placeholders that are not real marketplace ids."""
    if value in (None, ""):
        return True
    text = str(value).strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered in {"true", "false", "none", "null", "ok", "success"}:
        return True
    # Older send handlers could stringify an object returned in `result`.
    if text.startswith("{") or text.startswith("["):
        return True
    if text.startswith("local:") or text.startswith("crm:"):
        return True
    return False


def _raw_marks_crm_sent(raw_json: str | None) -> bool:
    return bool(raw_json and "_crm_sent_from_crm" in raw_json)


def _find_outbound_echo_candidate_conn(conn, *, chat_id: int, text: str, created_at: str | None, window_seconds: int = 900, exclude_id: int | None = None):
    """Find a local CRM outbound row that should be upgraded by a marketplace echo.

    Some marketplace send endpoints (notably Ozon/WB variants) acknowledge a send
    without returning the final message id that later appears in history. The CRM
    creates a local outbound row immediately, then sync imports the marketplace
    echo as a second outbound row. Match the echo to the local row by chat/text/time
    and upgrade that row instead of inserting a duplicate.
    """
    needle = (text or "").strip()
    if not needle or needle == "[сообщение без текста / вложение]":
        return None
    safe_window = max(30, min(int(window_seconds or 900), 86400))
    sql = """
        SELECT id, external_message_id, author, raw_json, created_at
        FROM messages
        WHERE chat_id=?
          AND direction='outbound'
          AND TRIM(text)=TRIM(?)
          AND ABS(strftime('%s', COALESCE(?, created_at)) - strftime('%s', created_at)) <= ?
    """
    params: list[Any] = [int(chat_id), needle, created_at, safe_window]
    if exclude_id is not None:
        sql += " AND id<>?"
        params.append(int(exclude_id))
    sql += " ORDER BY id DESC LIMIT 20"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        external_id = row["external_message_id"]
        raw_json = row["raw_json"] or "{}"
        if _is_provisional_outbound_external_id(external_id) or _raw_marks_crm_sent(raw_json):
            return row
    return None



def _find_existing_marketplace_echo_for_crm_send_conn(conn, *, chat_id: int, text: str, created_at: str | None, window_seconds: int = 900):
    """Find a marketplace echo that arrived before the CRM local-send row.

    Race this fixes:
    1. The operator sends a message from CRM.
    2. Browser autosync / marketplace sync imports the just-sent seller message first.
    3. The send endpoint then saves the local CRM row with no final marketplace id.

    Without this reverse lookup the UI briefly shows two outbound bubbles until the
    repair job removes one. Returning the existing echo lets add_message update it
    immediately, so the duplicate is filtered before it appears.
    """
    needle = (text or "").strip()
    if not needle or needle == "[сообщение без текста / вложение]":
        return None
    safe_window = max(30, min(int(window_seconds or 900), 86400))
    rows = conn.execute(
        """
        SELECT id, external_message_id, author, raw_json, created_at
        FROM messages
        WHERE chat_id=?
          AND direction='outbound'
          AND TRIM(text)=TRIM(?)
          AND ABS(strftime('%s', COALESCE(?, created_at)) - strftime('%s', created_at)) <= ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (int(chat_id), needle, created_at, safe_window),
    ).fetchall()
    for row in rows:
        raw_json = row["raw_json"] or "{}"
        # Existing echo must look like marketplace data, not another CRM local row.
        if _raw_marks_crm_sent(raw_json):
            continue
        if _is_provisional_outbound_external_id(row["external_message_id"]):
            continue
        return row
    return None

def repair_outbound_marketplace_echo_duplicates(limit: int = 1000, window_seconds: int = 900) -> int:
    """Merge already-created duplicate outbound echoes back into the CRM local row.

    This is a repair for rows created before the echo-upgrade logic existed. It is
    intentionally conservative: only same chat + same text + close timestamps +
    outbound direction, and only when one candidate looks like a local/provisional
    CRM send.
    """
    safe_limit = max(1, min(int(limit or 1000), 10000))
    safe_window = max(30, min(int(window_seconds or 900), 86400))
    repaired = 0
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, external_message_id, author, text, created_at, raw_json
            FROM messages
            WHERE direction='outbound'
              AND TRIM(text)<>''
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        for row in rows:
            row_id = int(row["id"])
            external_id = row["external_message_id"]
            raw_json = row["raw_json"] or "{}"
            # We treat a non-provisional, non-CRM-marked row as the marketplace echo.
            if _is_provisional_outbound_external_id(external_id) or _raw_marks_crm_sent(raw_json):
                continue
            local = _find_outbound_echo_candidate_conn(
                conn,
                chat_id=int(row["chat_id"]),
                text=row["text"] or "",
                created_at=row["created_at"],
                window_seconds=safe_window,
                exclude_id=row_id,
            )
            if not local:
                continue
            local_id = int(local["id"])
            conn.execute(
                """
                UPDATE messages
                SET external_message_id=?,
                    author=COALESCE(NULLIF(author, ''), ?),
                    raw_json=?,
                    created_at=COALESCE(?, created_at)
                WHERE id=?
                """,
                (external_id, row["author"], raw_json, row["created_at"], local_id),
            )
            conn.execute("DELETE FROM messages WHERE id=?", (row_id,))
            refresh_chat_last_message(conn, int(row["chat_id"]))
            repaired += 1
    return repaired

def delete_mock_chats() -> int:
    """Remove historical demo/mock chats from local databases.

    Mock data was useful for early UI testing, but it must not appear in the working CRM.
    """
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM chats WHERE marketplace='mock' OR metadata_json LIKE '%\"source\": \"mock\"%'")
        return int(cur.rowcount or 0)




def _payload_contains_token(payload: Any, tokens: tuple[str, ...], depth: int = 0) -> bool:
    if depth > 7 or payload in (None, ""):
        return False
    if isinstance(payload, str):
        lowered = payload.lower()
        return any(token in lowered for token in tokens)
    if isinstance(payload, (int, float, bool)):
        return False
    if isinstance(payload, list):
        return any(_payload_contains_token(item, tokens, depth + 1) for item in payload[:100])
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_l = str(key).lower()
            if any(token in key_l for token in tokens):
                return True
            if _payload_contains_token(value, tokens, depth + 1):
                return True
    return False


def _ozon_notification_tokens() -> tuple[str, ...]:
    return tuple(
        token.strip().lower()
        for token in __import__("os").getenv(
            "OZON_NOTIFICATION_USER_MARKERS",
            "notificationuser,notification_user,systemuser,system_user",
        ).split(",")
        if token.strip()
    )


def _normalize_system_sender(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def _system_sender_matches(value: Any, markers: tuple[str, ...]) -> bool:
    normalized = _normalize_system_sender(value)
    if not normalized:
        return False
    normalized_markers = {_normalize_system_sender(marker) for marker in markers if marker}
    return normalized in normalized_markers


def _extract_sender_designations(value: Any, depth: int = 0) -> list[str]:
    if depth > 4 or value in (None, ""):
        return []
    values: list[str] = []
    name_keys = {
        "name", "login", "username", "user_name", "display_name", "displayname",
        "nickname", "author_name", "authorname", "sender_name", "sendername",
        "from_name", "fromname", "system_name", "systemname",
    }
    container_keys = {"user", "author", "sender", "from", "participant", "profile"}
    if isinstance(value, dict):
        for key, nested in value.items():
            key_l = str(key).lower()
            if key_l in name_keys and isinstance(nested, (str, int, float)):
                values.append(str(nested))
            elif key_l in container_keys:
                if isinstance(nested, (str, int, float)):
                    values.append(str(nested))
                elif isinstance(nested, dict):
                    values.extend(_extract_sender_designations(nested, depth + 1))
    elif isinstance(value, list):
        for item in value[:20]:
            values.extend(_extract_sender_designations(item, depth + 1))
    return values


def _metadata_looks_like_ozon_notification_user(metadata: dict[str, Any]) -> bool:
    if __import__("os").getenv("OZON_EXCLUDE_NOTIFICATIONUSER_CHATS", "1").strip().lower() in {"0", "false", "no", "off", "нет"}:
        return False
    markers = _ozon_notification_tokens()
    return any(_system_sender_matches(value, markers) for value in _extract_sender_designations(metadata))


def _raw_json_sender_matches(raw_json: str | None, markers: tuple[str, ...]) -> bool:
    try:
        payload = json.loads(raw_json or "{}")
    except Exception:
        payload = {}
    return any(_system_sender_matches(value, markers) for value in _extract_sender_designations(payload))


def delete_chats_by_ids(chat_ids: list[int]) -> int:
    """Delete chats and dependent local data without relying on SQLite FK pragma."""
    ids = [int(i) for i in chat_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        conn.execute(f"UPDATE reviews SET linked_chat_id=NULL WHERE linked_chat_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM tasks WHERE chat_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM messages WHERE chat_id IN ({placeholders})", ids)
        cur = conn.execute(f"DELETE FROM chats WHERE id IN ({placeholders})", ids)
        return int(cur.rowcount or 0)




def mark_ozon_chat_as_system(chat_id: int, reason: str = "system_dialog") -> None:
    """Store a small marker before deleting/hiding, useful for diagnostics/logging.

    The chat is normally deleted right after this call, but keeping this function
    separates classification from removal and avoids customer-name based filters.
    """
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT metadata_json FROM chats WHERE id=?", (chat_id,)).fetchone()
            if not row:
                return
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            metadata["_crm_excluded_as_system"] = True
            metadata["_crm_excluded_reason"] = reason
            conn.execute(
                "UPDATE chats SET metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), chat_id),
            )
    except Exception:
        return


def _metadata_is_excluded_as_system(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("_crm_excluded_as_system") or metadata.get("crm_excluded_as_system"))


def chat_is_excluded_as_system(chat: dict[str, Any] | None) -> bool:
    if not isinstance(chat, dict):
        return False
    metadata = chat.get("metadata") if isinstance(chat.get("metadata"), dict) else None
    if metadata is None and "metadata_json" in chat:
        try:
            metadata = json.loads(chat.get("metadata_json") or "{}")
        except Exception:
            metadata = {}
    return _metadata_is_excluded_as_system(metadata)


def _system_excluded_condition_sql(alias: str = "c") -> str:
    return (
        f"COALESCE({alias}.metadata_json, '{{}}') LIKE '%\"_crm_excluded_as_system\": true%' "
        f"OR COALESCE({alias}.metadata_json, '{{}}') LIKE '%\"_crm_excluded_as_system\":true%'"
    )


def hide_ozon_system_chat_ids(chat_ids: list[int], reason: str = "system_dialog") -> int:
    ids = [int(i) for i in chat_ids if i]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        rows = conn.execute(f"SELECT id, metadata_json FROM chats WHERE id IN ({placeholders})", ids).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            metadata["_crm_excluded_as_system"] = True
            metadata["_crm_excluded_reason"] = reason
            conn.execute(
                "UPDATE chats SET metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), int(row["id"])),
            )
        return len(rows)


def _metadata_looks_like_ozon_support(metadata: dict[str, Any]) -> bool:
    if not isinstance(metadata, dict):
        return False
    chat = metadata.get("chat") if isinstance(metadata.get("chat"), dict) else metadata
    sync_hint = metadata.get("_sync_hint") if isinstance(metadata.get("_sync_hint"), dict) else {}

    if _metadata_looks_like_ozon_notification_user(metadata):
        return True

    explicit = sync_hint.get("is_customer_chat")
    if explicit is False:
        return True
    if explicit is True:
        return False

    def val(*keys: str) -> str:
        parts = []
        for source in (chat, metadata, sync_hint):
            if not isinstance(source, dict):
                continue
            for key in keys:
                v = source.get(key)
                if v not in (None, ""):
                    parts.append(str(v))
        return " ".join(parts).lower()

    chat_type = val("chat_type", "chatType", "type", "category", "kind", "channel")
    title = val("title", "name", "subject", "caption", "chat_name", "chatName", "chat_title")
    combined = f"{chat_type} {title}"

    customer_markers = (
        "buyer", "customer", "client", "consumer", "покупател", "клиент",
        "order", "posting", "return", "claim", "dispute", "заказ", "возврат",
    )
    # v80: do not delete buyer chats just because Ozon metadata contains broad
    # words such as "service", "system", "notification", "news" or "обновлен".
    # Those words can be technical metadata on normal customer dialogs.
    explicit_support_markers = (
        "seller_support", "tech_support", "helpdesk", "служба поддержки",
        "поддержка продавца", "ozon support", "seller api", "api update",
        "o4d", "spotlight", "digest", "newsletter",
        "notificationuser", "notification_user", "systemuser", "system_user",
    )
    if any(marker in combined for marker in customer_markers):
        return False
    if any(marker in combined for marker in explicit_support_markers):
        return True
    if "поддерж" in title:
        return True
    return False


def delete_ozon_support_chats() -> int:
    """Hide exact Ozon system dialogs from CRM.

    Important: by default this function no longer deletes rows. It marks them as
    _crm_excluded_as_system so future syncs remember the dialog and do not show
    or reload it again. Set OZON_PURGE_SYSTEM_CHATS=1 only for manual cleanup.
    """
    if os.getenv("OZON_DELETE_SUPPORT_CHATS", "1").strip().lower() in {"0", "false", "no", "off", "нет"}:
        return 0

    to_hide: list[int] = []
    system_markers = _ozon_notification_tokens()
    chatbot_markers = tuple(
        token.strip().lower()
        for token in os.getenv("OZON_CHATBOT_MARKERS", os.getenv("OZON_FIRST_MESSAGE_SYSTEM_USER_MARKERS", "chatbot")).split(",")
        if token.strip()
    )
    exclude_chatbot_messages = os.getenv("OZON_EXCLUDE_CHATBOT_MESSAGES", "1").strip().lower() not in {"0", "false", "no", "off", "нет"}

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, metadata_json
            FROM chats
            WHERE marketplace='ozon'
            """
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if _metadata_looks_like_ozon_notification_user(metadata):
                to_hide.append(int(row["id"]))

        # notificationuser/systemuser: exact sender/user names anywhere.
        msg_rows = conn.execute(
            """
            SELECT c.id, m.id AS message_id, m.author, m.raw_json
            FROM chats c
            JOIN messages m ON m.chat_id = c.id
            WHERE c.marketplace='ozon'
            """
        ).fetchall()
        for row in msg_rows:
            cid = int(row["id"])
            if cid in to_hide:
                continue
            if _system_sender_matches(row["author"], system_markers) or _raw_json_sender_matches(row["raw_json"], system_markers):
                to_hide.append(cid)

        # chatbot: hide dialogs whose first sender is chatbot or whose whole
        # imported history consists only of chatbot/system senders. Mixed buyer
        # dialogs stay visible; their chatbot messages are removed below.
        first_rows = conn.execute(
            """
            SELECT c.id, m.author, m.raw_json
            FROM chats c
            JOIN messages m ON m.chat_id = c.id
            WHERE c.marketplace='ozon'
              AND m.id = (
                SELECT m2.id
                FROM messages m2
                WHERE m2.chat_id = c.id
                ORDER BY datetime(m2.created_at) ASC, m2.id ASC
                LIMIT 1
              )
            """
        ).fetchall()
        for row in first_rows:
            cid = int(row["id"])
            if cid in to_hide:
                continue
            if _system_sender_matches(row["author"], chatbot_markers) or _raw_json_sender_matches(row["raw_json"], chatbot_markers):
                to_hide.append(cid)

        by_chat: dict[int, list[bool]] = {}
        for row in msg_rows:
            cid = int(row["id"])
            is_system_sender = (
                _system_sender_matches(row["author"], system_markers)
                or _raw_json_sender_matches(row["raw_json"], system_markers)
                or _system_sender_matches(row["author"], chatbot_markers)
                or _raw_json_sender_matches(row["raw_json"], chatbot_markers)
            )
            by_chat.setdefault(cid, []).append(is_system_sender)
        for cid, flags in by_chat.items():
            if cid not in to_hide and flags and all(flags):
                to_hide.append(cid)

        if exclude_chatbot_messages:
            chatbot_message_ids = []
            for row in msg_rows:
                if _system_sender_matches(row["author"], chatbot_markers) or _raw_json_sender_matches(row["raw_json"], chatbot_markers):
                    chatbot_message_ids.append(int(row["message_id"]))
            if chatbot_message_ids:
                placeholders = ",".join("?" for _ in chatbot_message_ids)
                conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", chatbot_message_ids)

    hidden = hide_ozon_system_chat_ids(to_hide, reason="startup_system_sender")
    if os.getenv("OZON_PURGE_SYSTEM_CHATS", "0").strip().lower() in {"1", "true", "yes", "on", "да"}:
        return delete_chats_by_ids(to_hide)
    return hidden

def upsert_chat(chat: ChatCreate) -> int:
    with get_connection() as conn:
        incoming_metadata = dict(chat.metadata or {})
        existing = conn.execute(
            "SELECT status, metadata_json FROM chats WHERE marketplace=? AND external_chat_id=?",
            (chat.marketplace, chat.external_chat_id),
        ).fetchone()
        if existing:
            try:
                existing_metadata = json.loads(existing["metadata_json"] or "{}")
            except Exception:
                existing_metadata = {}
            # Marketplace sync brings fresh raw metadata, but must not erase CRM
            # workflow markers. Losing _crm_status_manual caused closed chats to
            # return to the active inbox after the next background sync.
            for key, value in existing_metadata.items():
                if str(key).startswith("_crm_"):
                    incoming_metadata[key] = value
            if _is_closed_status_key_conn(conn, existing["status"]):
                incoming_metadata["_crm_status_manual"] = True
                incoming_metadata.setdefault("_crm_status_manual_value", "closed")
                incoming_metadata.setdefault("_crm_status_manual_source_value", existing["status"])
        conn.execute(
            """
            INSERT INTO chats (
                marketplace, external_chat_id, customer_name, customer_public_id,
                order_id, status, assigned_to, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marketplace, external_chat_id) DO UPDATE SET
                customer_name=CASE
                    WHEN chats.metadata_json LIKE '%"_crm_customer_name_manual": true%' THEN chats.customer_name
                    ELSE COALESCE(NULLIF(excluded.customer_name, ''), chats.customer_name)
                END,
                customer_public_id=COALESCE(NULLIF(excluded.customer_public_id, ''), chats.customer_public_id),
                order_id=COALESCE(NULLIF(excluded.order_id, ''), order_id),
                -- Marketplace sync must not erase operator workflow fields, except
                -- when a marketplace explicitly reports unread activity. In that case
                -- a previously archived/closed chat must return to the active inbox.
                status=CASE
                    -- Manual CRM status and closed-like workflow statuses have priority
                    -- over marketplace sync. Without this, background sync could reset
                    -- closed dialogs back to "new" when marketplace metadata contains
                    -- unread flags from an already imported message.
                    WHEN chats.metadata_json LIKE '%"_crm_status_manual": true%' THEN chats.status
                    WHEN chats.status='closed' THEN chats.status
                    WHEN lower(chats.status) IN ('closed', 'archive', 'archived', 'zakryt', 'zakryto') THEN chats.status
                    WHEN chats.status LIKE '%Закры%' OR chats.status LIKE '%закры%' THEN chats.status
                    WHEN chats.status IN (
                        SELECT key FROM chat_statuses
                        WHERE key='closed'
                           OR lower(key) IN ('closed', 'archive', 'archived', 'zakryt', 'zakryto')
                           OR title LIKE '%Закры%'
                           OR title LIKE '%закры%'
                    ) THEN chats.status
                    -- Custom statuses are not known to marketplace sync, so never
                    -- overwrite them from unread_count / first_unread_message_id.
                    WHEN chats.status NOT IN ('new', 'in_progress', 'waiting_customer', 'closed') THEN chats.status
                    WHEN excluded.metadata_json LIKE '%"unread_count": 0%' THEN chats.status
                    WHEN excluded.metadata_json LIKE '%"unread_count":%' THEN 'new'
                    WHEN excluded.metadata_json LIKE '%"first_unread_message_id":%' AND excluded.metadata_json NOT LIKE '%"first_unread_message_id": null%' THEN 'new'
                    ELSE chats.status
                END,
                assigned_to=chats.assigned_to,
                metadata_json=excluded.metadata_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                chat.marketplace,
                chat.external_chat_id,
                chat.customer_name,
                chat.customer_public_id,
                chat.order_id,
                chat.status,
                chat.assigned_to,
                json.dumps(incoming_metadata, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT id FROM chats WHERE marketplace=? AND external_chat_id=?",
            (chat.marketplace, chat.external_chat_id),
        ).fetchone()
        return int(row["id"])


def _chat_has_manual_customer_name(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("_crm_customer_name_manual") or metadata.get("customer_name_manual"))


def _chat_metadata(chat_id: int) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT metadata_json FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["metadata_json"] or "{}")
    except Exception:
        return {}


def update_chat_customer_info(chat_id: int, customer_name: str | None = None, customer_public_id: str | None = None) -> None:
    """Update customer fields from marketplace sync without overwriting manual CRM names."""
    metadata = _chat_metadata(chat_id)
    assignments: list[str] = []
    params: list[Any] = []

    # If an operator set the client name in CRM, background sync must not erase it
    # or replace it with Ozon fallback names such as "Клиент ab12cd34".
    if customer_name and customer_name.strip() and not _chat_has_manual_customer_name(metadata):
        assignments.append("customer_name=?")
        params.append(customer_name.strip())
    if customer_public_id and customer_public_id.strip():
        assignments.append("customer_public_id=COALESCE(NULLIF(customer_public_id, ''), ?)")
        params.append(customer_public_id.strip())
    if not assignments:
        return
    params.append(chat_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE chats SET {', '.join(assignments)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )


def _message_preview(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw:
        return "[вложение]"

    # Clean marketplace image placeholders/links for the chat list. Operators need
    # a readable preview instead of raw Markdown or API file URLs.
    import re
    if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)", raw, flags=re.I):
        return "Изображение"
    without_images = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", raw).strip()
    if not without_images and without_images != raw:
        return "Изображение"
    if re.fullmatch(r"https?://[^\s<>\"']+\.(jpg|jpeg|png|webp|gif|bmp|svg)(\?.*)?", without_images or raw, flags=re.I):
        return "Изображение"
    if re.search(r"https?://api-seller\.ozon\.ru/v\d+/chat/file/", without_images or raw, flags=re.I):
        return "Изображение"

    preview = (without_images or raw)[:180]
    return preview or "Изображение"


def _utc_now_iso() -> str:
    return __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"




def _truthy_marketplace_flag(value: Any) -> bool:
    if value is True:
        return True
    if value in (False, None, ""):
        return False
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "да", "new", "unread", "not_read", "notread"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "нет", "read", "seen", "viewed", "opened"}:
            return False
    return False


def _find_first_nested_key(payload: Any, key_names: set[str], depth: int = 0) -> Any:
    """Find the first value for one of key_names in marketplace raw payload."""
    if depth > 7 or payload in (None, ""):
        return None
    normalized = {str(k).lower().replace("_", "").replace("-", "") for k in key_names}
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_norm = str(key).lower().replace("_", "").replace("-", "")
            if key_norm in normalized:
                return value
        for value in payload.values():
            found = _find_first_nested_key(value, key_names, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for item in payload[:80]:
            found = _find_first_nested_key(item, key_names, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _chat_metadata_reports_unread(metadata: dict[str, Any] | None) -> bool:
    """Return True only when marketplace explicitly reports unread/new customer activity.

    We intentionally do not use CRM status alone. A chat can have status "new" for
    workflow reasons, while the marketplace has already marked the dialog as read
    or the manager has already answered.
    """
    if not isinstance(metadata, dict):
        return False

    sync_hint = metadata.get("_sync_hint") if isinstance(metadata.get("_sync_hint"), dict) else {}

    # Yandex Market does not always expose a classic unread_count in the chat list.
    # Its chat status is the actionable signal for CRM:
    # NEW / WAITING_FOR_PARTNER = partner should answer; WAITING_FOR_CUSTOMER / FINISHED = no unread action.
    yandex_status = str(
        sync_hint.get("yandex_chat_status")
        or sync_hint.get("chat_status")
        or metadata.get("status")
        or metadata.get("chatStatus")
        or metadata.get("state")
        or ""
    ).strip().upper()
    if yandex_status in {"NEW", "WAITING_FOR_PARTNER", "WAITING_FOR_PARTNER_RESPONSE", "PARTNER_REQUIRED"}:
        return True
    if yandex_status in {"WAITING_FOR_CUSTOMER", "FINISHED", "CLOSED", "RESOLVED"}:
        return False
    if _truthy_marketplace_flag(sync_hint.get("yandex_needs_partner_reply")):
        return True

    unread_count = (
        sync_hint.get("unread_count")
        if isinstance(sync_hint, dict) and "unread_count" in sync_hint
        else _find_first_nested_key(metadata, {
            "unread_count", "unreadCount", "unread_messages_count", "unreadMessagesCount",
            "new_messages_count", "newMessagesCount",
        })
    )
    try:
        if unread_count not in (None, "") and int(unread_count) > 0:
            return True
    except Exception:
        if _truthy_marketplace_flag(unread_count):
            return True

    first_unread = (
        sync_hint.get("first_unread_message_id")
        if isinstance(sync_hint, dict) and "first_unread_message_id" in sync_hint
        else _find_first_nested_key(metadata, {"first_unread_message_id", "firstUnreadMessageId", "first_unread_id"})
    )
    if first_unread not in (None, "", 0, "0", False):
        return True

    for key in (
        "isNewChat", "is_new_chat", "isUnread", "is_unread", "hasUnread", "has_unread",
        "unread", "newChat", "new_chat",
    ):
        value = _find_first_nested_key(metadata, {key})
        if _truthy_marketplace_flag(value):
            return True

    return False


def _chat_row_reports_unread(chat_row) -> bool:
    if not chat_row:
        return False
    try:
        metadata = json.loads(chat_row["metadata_json"] or "{}")
    except Exception:
        metadata = {}
    return _chat_metadata_reports_unread(metadata)


def _message_raw_looks_seller_side(raw_payload: dict[str, Any] | None) -> bool:
    """Extra protection when connector direction is ambiguous."""
    if not isinstance(raw_payload, dict):
        return False
    for key in (
        "is_seller", "isSeller", "from_seller", "fromSeller",
        "is_supplier", "isSupplier", "from_supplier", "fromSupplier",
        "is_operator", "isOperator",
    ):
        value = _find_first_nested_key(raw_payload, {key})
        if _truthy_marketplace_flag(value):
            return True

    side = _find_first_nested_key(raw_payload, {
        "sender", "senderType", "author", "authorType", "userType", "source", "from",
        "side", "role", "type", "participantType", "participant_type",
    })
    if isinstance(side, str):
        lowered = side.strip().lower()
        if lowered in {
            "seller", "продавец", "supplier", "vendor", "shop", "merchant",
            "manager", "operator", "admin", "support", "employee", "staff",
        }:
            return True
    return False


def _latest_message_row_conn(conn, chat_id: int):
    return conn.execute(
        """
        SELECT id, direction, created_at
        FROM messages
        WHERE chat_id=?
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 1
        """,
        (int(chat_id),),
    ).fetchone()


def _resolve_stale_message_notifications_conn(conn, chat_id: int | None = None) -> int:
    """Mark message notifications as read when the chat no longer waits for reply.

    A marketplace sync can import an old inbound message after the manager has
    already answered it. Such messages can still be recent, but they should not
    keep appearing as notifications. For "new_message" notifications we keep only
    the notification that points to the current latest inbound message of the chat.
    """
    params: list[Any] = []
    chat_filter = ""
    if chat_id is not None:
        chat_filter = " AND n.chat_id=?"
        params.append(int(chat_id))

    cur = conn.execute(
        f"""
        UPDATE notifications AS n
        SET is_read=1, read_at=CURRENT_TIMESTAMP
        WHERE n.is_read=0
          AND n.type='new_message'
          AND n.chat_id IS NOT NULL
          {chat_filter}
          AND (
            NOT EXISTS (SELECT 1 FROM chats c WHERE c.id=n.chat_id)
            OR EXISTS (SELECT 1 FROM chats c WHERE c.id=n.chat_id AND c.status='closed')
            OR EXISTS (
                SELECT 1
                FROM chats c
                WHERE c.id=n.chat_id
                  AND (
                    c.metadata_json LIKE '%"unread_count": 0%'
                    OR c.metadata_json LIKE '%"unreadCount": 0%'
                    OR c.metadata_json LIKE '%"isNewChat": false%'
                    OR c.metadata_json LIKE '%"is_new_chat": false%'
                  )
                  AND c.metadata_json NOT LIKE '%"first_unread_message_id":%'
                  AND c.metadata_json NOT LIKE '%"firstUnreadMessageId":%'
            )
            OR COALESCE((
                SELECT m.direction
                FROM messages m
                WHERE m.chat_id=n.chat_id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ), '') != 'inbound'
            OR COALESCE(CAST(n.entity_id AS INTEGER), -1) != COALESCE((
                SELECT m.id
                FROM messages m
                WHERE m.chat_id=n.chat_id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ), -2)
          )
        """,
        params,
    )
    return int(cur.rowcount or 0)


def refresh_chat_last_message(conn, chat_id: int) -> None:
    """Rebuild chat last-message cache from the real newest message.

    Background sync can receive old history again. Updating the chat preview with
    every imported message makes the list show an older customer message after our
    later manager reply. This cache is now derived from the actual newest message
    by created_at/id, so the chat list always shows the true latest message.
    """
    row = conn.execute(
        """
        SELECT id, direction, text, created_at
        FROM messages
        WHERE chat_id=?
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 1
        """,
        (chat_id,),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE chats
            SET last_message_at=?, last_message_preview=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (row["created_at"], _message_preview(row["text"]), chat_id),
        )
    else:
        conn.execute(
            """
            UPDATE chats
            SET last_message_at=NULL, last_message_preview=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (chat_id,),
        )

    _resolve_stale_message_notifications_conn(conn, int(chat_id))


def normalize_legacy_outbound_timestamps() -> int:
    """Mark old CRM-created outbound/internal timestamps as UTC.

    Earlier versions let SQLite fill CURRENT_TIMESTAMP, which is UTC but has no
    timezone suffix. Browsers interpreted it as local time, and SQLite sorting
    compared it incorrectly with marketplace ISO/Z timestamps. This made newly
    sent manager replies appear older than the previous customer message.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at
            FROM messages
            WHERE direction IN ('outbound','internal')
              AND created_at GLOB '????-??-?? ??:??:??'
            """
        ).fetchall()
        for row in rows:
            normalized = str(row["created_at"]).replace(" ", "T") + "Z"
            conn.execute("UPDATE messages SET created_at=? WHERE id=?", (normalized, int(row["id"])))
        return len(rows)


def repair_chat_last_message_cache() -> int:
    """Repair stale last-message previews for existing local databases."""
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM chats WHERE marketplace != 'mock'").fetchall()
        for row in rows:
            refresh_chat_last_message(conn, int(row["id"]))
        return len(rows)



def _normalized_wb_field_name(key: Any) -> str:
    return str(key or "").strip().lower().replace("_", "").replace("-", "").replace("с", "c")


def _direct_wb_client_name_marker(payload: dict[str, Any] | None) -> tuple[bool, Any, str | None]:
    """Find a direct WB clientName/client_name key, preserving empty values."""
    if not isinstance(payload, dict):
        return False, None, None
    for key, value in payload.items():
        if _normalized_wb_field_name(key) == "clientname":
            return True, value, str(key)
    return False, None, None


def _wb_lastmessage_direction_from_raw(raw: dict[str, Any] | None) -> tuple[str | None, dict[str, Any] | None]:
    """Return direction for WB chat-list lastMessage from the surrounding chat item.

    WB /seller/chats echoes lastMessage without a stable sender field. In the
    payloads observed in production, the direct clientName on the surrounding
    chat item is empty for buyer lastMessage and filled with buyer name for
    seller lastMessage. This function is intentionally restricted to
    _crm_source=wb_lastMessage rows so it never affects full WB event history.
    """
    if not isinstance(raw, dict):
        return None, None
    source = str(raw.get("_crm_source") or "").strip().lower()
    if source != "wb_lastmessage":
        return None, None

    candidates: list[tuple[str, dict[str, Any]]] = []
    chat_item = raw.get("_chat_item")
    if isinstance(chat_item, dict):
        candidates.append(("chat_item", chat_item))
    candidates.append(("lastMessage", raw))

    for scope, obj in candidates:
        has_marker, value, field = _direct_wb_client_name_marker(obj)
        if not has_marker:
            continue
        direction = "outbound" if str(value or "").strip() else "inbound"
        marker = {
            "scope": scope,
            "field": field,
            "value_present": bool(str(value or "").strip()),
            "resolved_direction": direction,
            "repaired_by": "repository.repair_wb_lastmessage_directions",
        }
        return direction, marker
    return None, None


def repair_wb_lastmessage_directions() -> int:
    """Repair already imported WB lastMessage rows after direction-rule changes.

    Earlier builds could save WB chat-list lastMessage as inbound before the
    clientName marker was known. Those rows keep chats in "ждёт ответа" until
    the stored message direction is corrected. This repair is safe to run on
    startup and after WB sync; it only touches rows explicitly marked as
    _crm_source=wb_lastMessage.
    """
    changed = 0
    affected_chat_ids: set[int] = set()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, direction, author, raw_json
            FROM messages
            WHERE raw_json LIKE '%wb_lastMessage%'
              AND raw_json LIKE '%_chat_item%'
            """
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except Exception:
                continue
            direction, marker = _wb_lastmessage_direction_from_raw(raw)
            if direction not in {"inbound", "outbound"}:
                continue
            if marker:
                raw["_crm_wb_client_name_direction_marker"] = marker
            current_direction = str(row["direction"] or "")
            current_raw = row["raw_json"] or ""
            new_raw = json.dumps(raw, ensure_ascii=False)
            needs_update = current_direction != direction or new_raw != current_raw
            if not needs_update:
                continue
            author = "seller" if direction == "outbound" else "customer"
            conn.execute(
                """
                UPDATE messages
                SET direction=?, author=?, raw_json=?
                WHERE id=?
                """,
                (direction, author, new_raw, int(row["id"])),
            )
            changed += 1
            affected_chat_ids.add(int(row["chat_id"]))

        for chat_id in affected_chat_ids:
            refresh_chat_last_message(conn, chat_id)
    return changed


def reopen_closed_chat_for_new_activity(chat_id: int, latest_direction: str | None = None) -> bool:
    """Move archived chat back to active inbox when a marketplace reports new activity.

    We only call this after a sync pass has confirmed newer messages for that chat,
    so old historical imports will not reopen archived conversations accidentally.
    """
    new_status = "new" if latest_direction == "inbound" else "in_progress"
    with get_connection() as conn:
        row = conn.execute("SELECT status FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not row or row["status"] != "closed":
            return False
        conn.execute(
            "UPDATE chats SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, chat_id),
        )
        return True


def get_latest_message_for_chat(chat_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, direction, text, created_at, external_message_id
            FROM messages
            WHERE chat_id=?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return row_to_dict(row) if row else None



def _notification_preview(text: str | None, limit: int = 140) -> str:
    raw = (text or "").strip()
    if not raw:
        return "[без текста]"
    raw = re.sub(r"\s+", " ", raw)
    return raw[:limit].rstrip() + ("…" if len(raw) > limit else "")


def _parse_dt_for_notifications(value: str | None):
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    dt_mod = __import__("datetime")
    try:
        return dt_mod.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            return dt_mod.datetime.fromisoformat(raw.replace(" ", "T"))
        except Exception:
            return None


def _message_recent_enough_for_notification(created_at: str | None) -> bool:
    """Skip notifications for old history imported by backfill."""
    try:
        hours = max(1, min(720, int(os.getenv("CRM_NOTIFICATION_RECENT_MESSAGE_HOURS", "24"))))
    except Exception:
        hours = 24
    dt = _parse_dt_for_notifications(created_at)
    if not dt:
        return True
    dt_mod = __import__("datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_mod.timezone.utc)
    now = dt_mod.datetime.now(dt_mod.timezone.utc)
    return dt >= now - dt_mod.timedelta(hours=hours)


def _active_notification_user_ids(conn, *, include_viewers: bool = False) -> list[int]:
    roles = ("admin", "manager") if not include_viewers else ("admin", "manager", "viewer")
    placeholders = ",".join(["?"] * len(roles))
    rows = conn.execute(
        f"""
        SELECT id
        FROM users
        WHERE is_active=1 AND role IN ({placeholders})
        ORDER BY id ASC
        """,
        roles,
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _create_notification_conn(
    conn,
    *,
    user_id: int,
    type: str,
    title: str,
    body: str | None = None,
    chat_id: int | None = None,
    task_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    dedupe_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    if not user_id:
        return None
    raw_json = json.dumps(metadata or {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO notifications (
            user_id, type, title, body, chat_id, task_id,
            entity_type, entity_id, dedupe_key, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(type or "event"),
            str(title or "Уведомление"),
            body,
            chat_id,
            task_id,
            entity_type,
            str(entity_id) if entity_id not in (None, "") else None,
            dedupe_key,
            raw_json,
        ),
    )
    if cur.rowcount:
        return int(cur.lastrowid)
    return None


def create_notification(
    *,
    user_id: int,
    type: str,
    title: str,
    body: str | None = None,
    chat_id: int | None = None,
    task_id: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    dedupe_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    with get_connection() as conn:
        return _create_notification_conn(
            conn,
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            chat_id=chat_id,
            task_id=task_id,
            entity_type=entity_type,
            entity_id=entity_id,
            dedupe_key=dedupe_key,
            metadata=metadata,
        )


def _notify_new_inbound_message_conn(conn, *, chat_id: int, message_id: int, text: str, created_at: str | None) -> None:
    if not _message_recent_enough_for_notification(created_at):
        return
    chat = conn.execute(
        """
        SELECT c.id, c.marketplace, c.customer_name, c.customer_public_id,
               c.external_chat_id, c.assigned_user_id, c.status, c.metadata_json
        FROM chats c
        WHERE c.id=?
        """,
        (int(chat_id),),
    ).fetchone()
    if not chat or chat["status"] == "closed":
        return

    if not _chat_row_reports_unread(chat):
        # Marketplace did not mark this dialog as new/unread. Do not notify about
        # already-read or already-answered messages imported during sync.
        return

    message_row = conn.execute(
        "SELECT raw_json, direction FROM messages WHERE id=? AND chat_id=?",
        (int(message_id), int(chat_id)),
    ).fetchone()
    if message_row:
        try:
            raw_payload = json.loads(message_row["raw_json"] or "{}")
        except Exception:
            raw_payload = {}
        if message_row["direction"] != "inbound" or _message_raw_looks_seller_side(raw_payload):
            return

    latest = _latest_message_row_conn(conn, int(chat_id))
    if not latest or latest["direction"] != "inbound" or int(latest["id"]) != int(message_id):
        # Do not notify about historical/read messages when the current chat state
        # already has a later manager reply.
        return

    customer = (chat["customer_name"] or chat["customer_public_id"] or chat["external_chat_id"] or "Клиент")
    body = _notification_preview(text)

    if chat["assigned_user_id"]:
        target_ids = [int(chat["assigned_user_id"])]
        title = f"Новое сообщение: {customer}"
    else:
        target_ids = _active_notification_user_ids(conn, include_viewers=False)
        title = f"Новое неназначенное сообщение: {customer}"

    for user_id in target_ids:
        _create_notification_conn(
            conn,
            user_id=user_id,
            type="new_message",
            title=title,
            body=body,
            chat_id=int(chat_id),
            entity_type="message",
            entity_id=str(message_id),
            dedupe_key=f"message:{message_id}:user:{user_id}",
            metadata={
                "marketplace": chat["marketplace"],
                "message_created_at": created_at,
            },
        )



def cleanup_read_marketplace_notifications() -> int:
    """Mark old message notifications read when marketplace says dialog is not unread."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT n.id, c.metadata_json
            FROM notifications n
            JOIN chats c ON c.id=n.chat_id
            WHERE n.is_read=0 AND n.type='new_message'
            """
        ).fetchall()
        ids: list[int] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}
            if not _chat_metadata_reports_unread(metadata):
                ids.append(int(row["id"]))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"UPDATE notifications SET is_read=1, read_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            ids,
        )
        return int(cur.rowcount or 0)


def list_notifications(user_id: int, *, limit: int = 30, unread_only: bool = False) -> dict[str, Any]:
    limit = max(1, min(int(limit or 30), 100))
    clauses = ["n.user_id=?"]
    params: list[Any] = [int(user_id)]
    if unread_only:
        clauses.append("n.is_read=0")
    where = " AND ".join(clauses)
    cleanup_read_marketplace_notifications()
    with get_connection() as conn:
        _resolve_stale_message_notifications_conn(conn)
        rows = conn.execute(
            f"""
            SELECT
                n.*,
                c.marketplace,
                c.customer_name,
                c.customer_public_id,
                c.external_chat_id,
                t.title AS task_title
            FROM notifications n
            LEFT JOIN chats c ON c.id = n.chat_id
            LEFT JOIN tasks t ON t.id = n.task_id
            WHERE {where}
            ORDER BY n.is_read ASC, datetime(n.created_at) DESC, n.id DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
        unread = conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND is_read=0",
            (int(user_id),),
        ).fetchone()["c"]
        return {"items": [row_to_dict(row) for row in rows], "unread_count": int(unread)}


def mark_notification_read(notification_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE notifications SET is_read=1, read_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
            (int(notification_id), int(user_id)),
        )
        return bool(cur.rowcount)


def mark_all_notifications_read(user_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE notifications SET is_read=1, read_at=CURRENT_TIMESTAMP WHERE user_id=? AND is_read=0",
            (int(user_id),),
        )
        return int(cur.rowcount or 0)


def add_message(
    chat_id: int,
    direction: str,
    text: str,
    author: str | None = None,
    external_message_id: str | None = None,
    raw: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    """Insert or update a marketplace message and rebuild chat preview safely."""
    clean_external_id = external_message_id or None
    raw_json = json.dumps(raw or {}, ensure_ascii=False)
    if not created_at:
        created_at = _utc_now_iso()

    with get_connection() as conn:
        message_id: int
        if direction == "outbound" and not clean_external_id and _raw_marks_crm_sent(raw_json):
            # Reverse race guard: if autosync already imported the marketplace
            # echo before this send request saved its local row, update that echo
            # in place instead of inserting a second CRM bubble.
            existing_echo = _find_existing_marketplace_echo_for_crm_send_conn(
                conn,
                chat_id=int(chat_id),
                text=text,
                created_at=created_at,
                window_seconds=int(__import__("os").getenv("CRM_OUTBOUND_ECHO_MATCH_WINDOW_SECONDS", "900") or "900"),
            )
            if existing_echo:
                merged_raw = raw or {}
                try:
                    echo_raw = json.loads(existing_echo["raw_json"] or "{}")
                    if isinstance(echo_raw, dict):
                        merged_raw = {**echo_raw, **(raw or {})}
                except Exception:
                    pass
                conn.execute(
                    """
                    UPDATE messages
                    SET author=COALESCE(NULLIF(?, ''), author),
                        raw_json=?,
                        created_at=COALESCE(created_at, ?)
                    WHERE id=?
                    """,
                    (author, json.dumps(merged_raw, ensure_ascii=False), created_at, existing_echo["id"]),
                )
                message_id = int(existing_echo["id"])
                refresh_chat_last_message(conn, chat_id)
                return message_id

        if clean_external_id and direction == "outbound":
            # Root-cause fix for duplicate seller replies: if the marketplace
            # returns the same CRM-sent message later with its real id, upgrade
            # the local/provisional outbound row instead of inserting a second
            # seller bubble. This runs before exact external-id lookup because
            # old local rows may have placeholder ids like "True" from send ACKs.
            outbound_echo_duplicate = _find_outbound_echo_candidate_conn(
                conn,
                chat_id=int(chat_id),
                text=text,
                created_at=created_at,
                window_seconds=int(__import__("os").getenv("CRM_OUTBOUND_ECHO_MATCH_WINDOW_SECONDS", "900") or "900"),
            )
            if outbound_echo_duplicate:
                conn.execute(
                    """
                    UPDATE messages
                    SET external_message_id=?,
                        author=COALESCE(NULLIF(author, ''), ?),
                        raw_json=?,
                        created_at=COALESCE(?, created_at)
                    WHERE id=?
                    """,
                    (clean_external_id, author, raw_json, created_at, outbound_echo_duplicate["id"]),
                )
                message_id = int(outbound_echo_duplicate["id"])
                refresh_chat_last_message(conn, chat_id)
                return message_id

        if clean_external_id:
            existing = conn.execute(
                "SELECT id, text FROM messages WHERE chat_id=? AND external_message_id=?",
                (chat_id, clean_external_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE messages
                    SET direction=?, author=?, text=?, raw_json=?,
                        created_at=COALESCE(?, created_at)
                    WHERE id=?
                    """,
                    (direction, author, text, raw_json, created_at, existing["id"]),
                )
                message_id = int(existing["id"])
                refresh_chat_last_message(conn, chat_id)
                return message_id

            # v64: older WB local repair builds saved lastMessage with a fallback
            # external id based on a missing/wrong timestamp. If we now parse
            # addTimestamp correctly, update the existing wb_lastMessage row
            # instead of inserting a duplicate.
            if str(clean_external_id).startswith("wb:last:") or '"_crm_source": "wb_lastMessage"' in raw_json:
                wb_last_duplicate = conn.execute(
                    """
                    SELECT id
                    FROM messages
                    WHERE chat_id=?
                      AND TRIM(text)=TRIM(?)
                      AND raw_json LIKE '%wb_lastMessage%'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (chat_id, text),
                ).fetchone()
                if wb_last_duplicate:
                    conn.execute(
                        """
                        UPDATE messages
                        SET external_message_id=?, direction=?, author=COALESCE(?, author), raw_json=?,
                            created_at=COALESCE(?, created_at)
                        WHERE id=?
                        """,
                        (clean_external_id, direction, author, raw_json, created_at, wb_last_duplicate["id"]),
                    )
                    message_id = int(wb_last_duplicate["id"])
                    refresh_chat_last_message(conn, chat_id)
                    return message_id

            # If WB direction detection is improved later, a fallback external id
            # that included the old direction can change. Update the same WB event
            # row by text/time instead of leaving an old inbound duplicate that keeps
            # the chat in "ждёт ответа".
            if str(clean_external_id).startswith("wb:") and "_crm_wb_msg_obj" in raw_json:
                wb_event_duplicate = conn.execute(
                    """
                    SELECT id
                    FROM messages
                    WHERE chat_id=?
                      AND TRIM(text)=TRIM(?)
                      AND raw_json LIKE '%_crm_wb_msg_obj%'
                      AND ABS(strftime('%s', COALESCE(?, created_at)) - strftime('%s', created_at)) <= 5
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (chat_id, text, created_at),
                ).fetchone()
                if wb_event_duplicate:
                    conn.execute(
                        """
                        UPDATE messages
                        SET external_message_id=?, direction=?, author=COALESCE(?, author), raw_json=?,
                            created_at=COALESCE(?, created_at)
                        WHERE id=?
                        """,
                        (clean_external_id, direction, author, raw_json, created_at, wb_event_duplicate["id"]),
                    )
                    message_id = int(wb_event_duplicate["id"])
                    refresh_chat_last_message(conn, chat_id)
                    return message_id

            # v50: de-duplicate seller replies. Some marketplace send endpoints
            # return no message id, so CRM first saved a local outbound message
            # with an empty external id. On the next sync the same seller reply
            # came back with a marketplace id and was inserted again. If the text
            # and direction match a recent local no-id message, upgrade that row
            # instead of creating a duplicate.
            local_duplicate = conn.execute(
                """
                SELECT id
                FROM messages
                WHERE chat_id=?
                  AND direction=?
                  AND COALESCE(external_message_id, '')=''
                  AND TRIM(text)=TRIM(?)
                  AND ABS(strftime('%s', COALESCE(?, created_at)) - strftime('%s', created_at)) <= 900
                ORDER BY id DESC
                LIMIT 1
                """,
                (chat_id, direction, text, created_at),
            ).fetchone()
            if local_duplicate:
                conn.execute(
                    """
                    UPDATE messages
                    SET external_message_id=?, author=COALESCE(?, author), raw_json=?,
                        created_at=COALESCE(?, created_at)
                    WHERE id=?
                    """,
                    (clean_external_id, author, raw_json, created_at, local_duplicate["id"]),
                )
                message_id = int(local_duplicate["id"])
                refresh_chat_last_message(conn, chat_id)
                return message_id

        # Also protect against double-clicks/retries when the marketplace response
        # still has no external id. Keep one local copy per same text/direction in
        # a short time window.
        if not clean_external_id:
            existing_local = conn.execute(
                """
                SELECT id
                FROM messages
                WHERE chat_id=?
                  AND direction=?
                  AND COALESCE(external_message_id, '')=''
                  AND TRIM(text)=TRIM(?)
                  AND ABS(strftime('%s', COALESCE(?, created_at)) - strftime('%s', created_at)) <= 30
                ORDER BY id DESC
                LIMIT 1
                """,
                (chat_id, direction, text, created_at),
            ).fetchone()
            if existing_local:
                refresh_chat_last_message(conn, chat_id)
                return int(existing_local["id"])

        cur = conn.execute(
            """
            INSERT INTO messages (chat_id, external_message_id, direction, author, text, created_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, clean_external_id, direction, author, text, created_at, raw_json),
        )

        message_id = int(cur.lastrowid)
        refresh_chat_last_message(conn, chat_id)
        if direction == "inbound":
            _notify_new_inbound_message_conn(
                conn,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                created_at=created_at,
            )
        return message_id


def _chats_select_sql(where: str) -> str:
    return f"""
        SELECT
            c.*,
            (SELECT u.display_name FROM users u WHERE u.id = c.assigned_user_id) AS assigned_user_display_name,
            (SELECT u.username FROM users u WHERE u.id = c.assigned_user_id) AS assigned_user_username,
            (
                SELECT m.direction
                FROM messages m
                WHERE m.chat_id = c.id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ) AS last_message_direction,
            (
                SELECT m.author
                FROM messages m
                WHERE m.chat_id = c.id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ) AS last_message_author,
            (
                SELECT m.text
                FROM messages m
                WHERE m.chat_id = c.id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ) AS actual_last_message_text,
            (
                SELECT m.created_at
                FROM messages m
                WHERE m.chat_id = c.id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ) AS actual_last_message_at,
            s.title AS status_title,
            s.color AS status_color,
            s.funnel_id AS funnel_id,
            f.title AS funnel_title
        FROM chats c
        LEFT JOIN chat_statuses s ON s.key = c.status
        LEFT JOIN chat_funnels f ON f.id = s.funnel_id
        {where}
        ORDER BY COALESCE(actual_last_message_at, c.last_message_at, c.updated_at, c.created_at) DESC
    """


def list_chats(status: str | None = None, marketplace: str | None = None, archived: bool = False, assigned_user_id: int | None = None, funnel_id: int | None = None) -> list[dict[str, Any]]:
    clauses = ["c.marketplace != 'mock'", f"NOT ({_system_excluded_condition_sql('c')})"]
    params: list[Any] = []
    closed_condition = _closed_status_condition_sql()

    # Main list shows active chats only. Closed-like statuses live in Archive.
    # This covers both built-in "closed" and custom statuses named "Закрыт".
    if archived:
        clauses.append(closed_condition)
    else:
        clauses.append(f"NOT {closed_condition}")
        if status and status != "closed":
            clauses.append("c.status = ?")
            params.append(status)

    if funnel_id:
        clauses.append("s.funnel_id = ?")
        params.append(int(funnel_id))
    if marketplace:
        clauses.append("c.marketplace = ?")
        params.append(marketplace)
    if assigned_user_id:
        clauses.append("c.assigned_user_id = ?")
        params.append(int(assigned_user_id))
    where = f"WHERE {' AND '.join(clauses)}"
    with get_connection() as conn:
        rows = conn.execute(_chats_select_sql(where), params).fetchall()
        # v40: do not additionally hide Ozon rows at list-render time.
        # System/support chats are filtered/deleted during sync; hiding here made
        # real customer chats disappear when old metadata was classified too broadly.
        return [_decorate_chat_sla(row_to_dict(r)) for r in rows]




def get_chat_by_external(marketplace: str, external_chat_id: str) -> dict[str, Any] | None:
    """Return a chat row by marketplace external id without loading full history."""
    with get_connection() as conn:
        row = conn.execute(
            _chats_select_sql("WHERE c.marketplace=? AND c.external_chat_id=? AND c.marketplace != 'mock'"),
            (marketplace, external_chat_id),
        ).fetchone()
        return _decorate_chat_sla(row_to_dict(row)) if row else None


def get_chat_summary(chat_id: int) -> dict[str, Any] | None:
    """Return a chat row without loading full message history.

    Used by quick UI updates such as status/assignee changes. Loading the whole
    dialog here makes mobile status edits feel slow and can race with navigation.
    """
    with get_connection() as conn:
        row = conn.execute(
            _chats_select_sql("WHERE c.id=? AND c.marketplace != 'mock'"),
            (int(chat_id),),
        ).fetchone()
        return _decorate_chat_sla(row_to_dict(row)) if row else None


def chat_has_messages(chat_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM messages WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone()
        return bool(row)


def get_chat(chat_id: int, messages_limit: int | None = None) -> dict[str, Any] | None:
    with get_connection() as conn:
        chat = conn.execute(_chats_select_sql("WHERE c.id=? AND c.marketplace != 'mock'"), (chat_id,)).fetchone()
        if not chat:
            return None
        chat_dict = row_to_dict(chat)
        if messages_limit and messages_limit > 0:
            messages = conn.execute(
                """
                SELECT * FROM (
                    SELECT *
                    FROM messages
                    WHERE chat_id=?
                    ORDER BY datetime(created_at) DESC, id DESC
                    LIMIT ?
                )
                ORDER BY datetime(created_at) ASC, id ASC
                """,
                (chat_id, int(messages_limit)),
            ).fetchall()
        else:
            messages = conn.execute(
                "SELECT * FROM messages WHERE chat_id=? ORDER BY datetime(created_at) ASC, id ASC",
                (chat_id,),
            ).fetchall()
        tasks = conn.execute(
            """
            SELECT t.*, u.display_name AS assignee_user_display_name, u.username AS assignee_user_username
            FROM tasks t
            LEFT JOIN users u ON u.id = t.assigned_user_id
            WHERE t.chat_id=?
            ORDER BY datetime(t.created_at) DESC, t.id DESC
            """,
            (chat_id,),
        ).fetchall()
        result = _decorate_chat_sla(chat_dict)
        result["messages"] = [row_to_dict(r) for r in messages]
        result["tasks"] = [row_to_dict(r) for r in tasks]
        return result


def update_chat(chat_id: int, payload: ChatUpdate) -> dict[str, Any] | None:
    # model_fields_set lets us distinguish "not sent" from "sent as null/empty".
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return get_chat_summary(chat_id)

    with get_connection() as conn:
        current = conn.execute("SELECT metadata_json FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not current:
            return None
        try:
            metadata = json.loads(current["metadata_json"] or "{}")
        except Exception:
            metadata = {}

        # Manual customer name from the CRM has priority over future marketplace sync.
        if "customer_name" in fields:
            name = (fields.get("customer_name") or "").strip()
            fields["customer_name"] = name or None
            metadata["_crm_customer_name_manual"] = bool(name)
            if name:
                metadata["_crm_customer_name_manual_at"] = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"
            fields["metadata_json"] = json.dumps(metadata, ensure_ascii=False)

        # Manual chat status from the CRM has priority over future marketplace sync.
        # Background sync still updates messages/previews, but does not reset the
        # operator's workflow status back to "new".
        if "status" in fields:
            status_value = str(fields.get("status") or "").strip()
            if status_value:
                canonical_status = _canonical_workflow_status_conn(conn, status_value)
                fields["status"] = canonical_status
                metadata["_crm_status_manual"] = True
                metadata["_crm_status_manual_value"] = canonical_status
                metadata["_crm_status_manual_source_value"] = status_value
                metadata["_crm_status_manual_at"] = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"
                fields["metadata_json"] = json.dumps(metadata, ensure_ascii=False)

        if "assigned_user_id" in fields:
            assigned_user_id = fields.get("assigned_user_id")
            if assigned_user_id in {"", 0}:
                assigned_user_id = None
            fields["assigned_user_id"] = assigned_user_id
            label = _get_user_label(conn, int(assigned_user_id)) if assigned_user_id else None
            # Keep the legacy assigned_to text in sync for old UI / reports.
            fields["assigned_to"] = label

            # Manual reassignment is always allowed and has priority over the
            # automatic "first responder" assignment. Auto-assign only works for
            # unassigned chats, so once an operator changes this field manually
            # background sync / later replies must not overwrite it.
            metadata["_crm_assigned_manual"] = True
            metadata["_crm_assigned_manual_user_id"] = int(assigned_user_id) if assigned_user_id else None
            metadata["_crm_assigned_manual_at"] = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"
            if label:
                metadata["_crm_assigned_manual_label"] = label
            else:
                metadata.pop("_crm_assigned_manual_label", None)
            fields["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
        elif "assigned_to" in fields and fields.get("assigned_to") == "":
            fields["assigned_to"] = None

        allowed = {"status", "assigned_to", "assigned_user_id", "customer_name", "metadata_json"}
        fields = {k: v for k, v in fields.items() if k in allowed}

        assignments = ", ".join([f"{key}=?" for key in fields])
        params = list(fields.values()) + [chat_id]
        conn.execute(
            f"UPDATE chats SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )
    return get_chat_summary(chat_id)



def assign_chat_to_user_if_unassigned(chat_id: int, user_id: int, reason: str = "first_reply") -> bool:
    """Assign an unassigned chat to the first CRM employee who replies.

    Safety rules:
    - never overwrite an existing `assigned_user_id`;
    - keep legacy `assigned_to` text in sync with the selected user;
    - store a small metadata marker for audit/debugging.

    This is used by the send-message endpoint so the chat immediately appears in
    the employee's "Мои чаты" tab after the first successful reply.
    """
    if not chat_id or not user_id:
        return False

    with get_connection() as conn:
        label = _get_user_label(conn, int(user_id))
        if not label:
            return False

        row = conn.execute(
            """
            SELECT id, assigned_user_id, metadata_json
            FROM chats
            WHERE id=?
            """,
            (int(chat_id),),
        ).fetchone()
        if not row:
            return False
        if row["assigned_user_id"] not in (None, "", 0):
            return False

        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        metadata["_crm_assigned_auto"] = True
        metadata["_crm_assigned_auto_reason"] = reason
        metadata["_crm_assigned_auto_user_id"] = int(user_id)
        metadata["_crm_assigned_auto_at"] = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"

        cur = conn.execute(
            """
            UPDATE chats
            SET assigned_user_id=?, assigned_to=?, metadata_json=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND (assigned_user_id IS NULL OR assigned_user_id='' OR assigned_user_id=0)
            """,
            (int(user_id), label, json.dumps(metadata, ensure_ascii=False), int(chat_id)),
        )
        return bool(cur.rowcount)


def get_chat_settings() -> dict[str, Any]:
    with get_connection() as conn:
        funnels = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM chat_funnels ORDER BY sort_order ASC, id ASC"
        ).fetchall()]
        statuses = [row_to_dict(r) for r in conn.execute(
            """
            SELECT s.*, f.title AS funnel_title
            FROM chat_statuses s
            LEFT JOIN chat_funnels f ON f.id=s.funnel_id
            ORDER BY COALESCE(f.sort_order, 9999) ASC, s.sort_order ASC, s.id ASC
            """
        ).fetchall()]
        return {"funnels": funnels, "statuses": statuses}


def create_chat_funnel(title: str, sort_order: int = 0) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("Название воронки не может быть пустым")
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO chat_funnels (title, sort_order, is_default) VALUES (?, ?, 0)",
            (clean_title, int(sort_order or 0)),
        )
        row = conn.execute("SELECT * FROM chat_funnels WHERE id=?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)


def update_chat_funnel(funnel_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed: dict[str, Any] = {}
    if "title" in fields and fields["title"] is not None:
        title = str(fields["title"] or "").strip()
        if title:
            allowed["title"] = title
    if "sort_order" in fields and fields["sort_order"] is not None:
        allowed["sort_order"] = int(fields["sort_order"] or 0)
    if "is_default" in fields and fields["is_default"] is not None:
        allowed["is_default"] = 1 if fields["is_default"] else 0
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM chat_funnels WHERE id=?", (funnel_id,)).fetchone():
            return None
        if allowed.get("is_default"):
            conn.execute("UPDATE chat_funnels SET is_default=0")
        if allowed:
            assignments = ", ".join([f"{key}=?" for key in allowed])
            conn.execute(f"UPDATE chat_funnels SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?", list(allowed.values()) + [funnel_id])
        row = conn.execute("SELECT * FROM chat_funnels WHERE id=?", (funnel_id,)).fetchone()
        return row_to_dict(row)


def delete_chat_funnel(funnel_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT is_default FROM chat_funnels WHERE id=?", (funnel_id,)).fetchone()
        if not row:
            return False
        if int(row["is_default"] or 0):
            raise ValueError("Нельзя удалить основную воронку")
        conn.execute("UPDATE chat_statuses SET funnel_id=NULL WHERE funnel_id=?", (funnel_id,))
        conn.execute("DELETE FROM chat_funnels WHERE id=?", (funnel_id,))
        return True


def create_chat_status(title: str, key: str | None = None, funnel_id: int | None = None, color: str | None = None, sort_order: int = 0) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("Название статуса не может быть пустым")
    with get_connection() as conn:
        if not funnel_id:
            row = conn.execute("SELECT id FROM chat_funnels WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()
            funnel_id = int(row["id"]) if row else None
        clean_key = _unique_status_key(conn, clean_title, key)
        cur = conn.execute(
            """
            INSERT INTO chat_statuses (key, title, funnel_id, color, sort_order, is_system, is_active)
            VALUES (?, ?, ?, ?, ?, 0, 1)
            """,
            (clean_key, clean_title, funnel_id, (color or "orange"), int(sort_order or 0)),
        )
        row = conn.execute(
            """
            SELECT s.*, f.title AS funnel_title
            FROM chat_statuses s
            LEFT JOIN chat_funnels f ON f.id=s.funnel_id
            WHERE s.id=?
            """,
            (cur.lastrowid,),
        ).fetchone()
        return row_to_dict(row)


def update_chat_status(status_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed: dict[str, Any] = {}
    if "title" in fields and fields["title"] is not None:
        title = str(fields["title"] or "").strip()
        if title:
            allowed["title"] = title
    if "funnel_id" in fields:
        allowed["funnel_id"] = int(fields["funnel_id"]) if fields.get("funnel_id") else None
    if "color" in fields and fields["color"] is not None:
        allowed["color"] = str(fields["color"] or "").strip() or None
    if "sort_order" in fields and fields["sort_order"] is not None:
        allowed["sort_order"] = int(fields["sort_order"] or 0)
    if "is_active" in fields and fields["is_active"] is not None:
        allowed["is_active"] = 1 if fields["is_active"] else 0
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM chat_statuses WHERE id=?", (status_id,)).fetchone():
            return None
        if allowed:
            assignments = ", ".join([f"{key}=?" for key in allowed])
            conn.execute(f"UPDATE chat_statuses SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?", list(allowed.values()) + [status_id])
        row = conn.execute(
            """
            SELECT s.*, f.title AS funnel_title
            FROM chat_statuses s
            LEFT JOIN chat_funnels f ON f.id=s.funnel_id
            WHERE s.id=?
            """,
            (status_id,),
        ).fetchone()
        return row_to_dict(row)


def delete_chat_status(status_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT key, is_system FROM chat_statuses WHERE id=?", (status_id,)).fetchone()
        if not row:
            return False
        in_use = conn.execute("SELECT COUNT(*) AS c FROM chats WHERE status=?", (row["key"],)).fetchone()["c"]
        # System/in-use statuses are deactivated so old chats do not break.
        if int(row["is_system"] or 0) or int(in_use or 0) > 0:
            conn.execute("UPDATE chat_statuses SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status_id,))
        else:
            conn.execute("DELETE FROM chat_statuses WHERE id=?", (status_id,))
        return True


def create_task(payload: TaskCreate) -> int:
    with get_connection() as conn:
        assigned_user_id = payload.assigned_user_id
        assignee = payload.assignee
        if assigned_user_id:
            assignee = _get_user_label(conn, int(assigned_user_id)) or assignee
        cur = conn.execute(
            """
            INSERT INTO tasks (chat_id, title, description, assignee, assigned_user_id, due_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (payload.chat_id, payload.title, payload.description, assignee, assigned_user_id, payload.due_at),
        )
        return int(cur.lastrowid)


def _load_task_comments(conn, task_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, task_id, comment, author, created_at
        FROM task_comments
        WHERE task_id=?
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        (task_id,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _decorate_task(row, conn) -> dict[str, Any]:
    data = row_to_dict(row)
    data["comments"] = _load_task_comments(conn, int(data["id"]))
    data["comments_count"] = len(data["comments"])
    data["last_comment"] = data["comments"][-1]["comment"] if data["comments"] else None
    if data.get("assigned_user_id"):
        data["assignee_user"] = {
            "id": data.get("assigned_user_id"),
            "display_name": data.get("assignee_user_display_name"),
            "username": data.get("assignee_user_username"),
        }
        if not data.get("assignee"):
            data["assignee"] = data.get("assignee_user_display_name") or data.get("assignee_user_username")
    return data


def update_task(task_id: int, payload: TaskUpdate) -> dict[str, Any] | None:
    fields = payload.model_dump(exclude_unset=True)
    comment = (fields.pop("comment", None) or "").strip()
    comment_author = (fields.pop("comment_author", None) or "manager").strip() or "manager"

    # Normalize empty strings from the UI.
    if "due_at" in fields and fields.get("due_at") in {"", None}:
        fields["due_at"] = None
    if "assignee" in fields and fields.get("assignee") == "":
        fields["assignee"] = None
    if "assigned_user_id" in fields and fields.get("assigned_user_id") in {"", 0}:
        fields["assigned_user_id"] = None
    if "description" in fields and fields.get("description") == "":
        fields["description"] = None

    status = fields.get("status")
    if status == "done":
        fields["completed_at"] = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"
        fields["archived_at"] = None
    elif status == "archived":
        fields["archived_at"] = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z"
    elif status in {"open", "in_progress", "cancelled"}:
        # Re-opening a task brings it back from archive/done sections.
        fields["completed_at"] = None
        fields["archived_at"] = None

    allowed = {"title", "description", "status", "assignee", "assigned_user_id", "due_at", "completed_at", "archived_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}

    with get_connection() as conn:
        exists = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not exists:
            return None
        if "assigned_user_id" in fields:
            assigned_user_id = fields.get("assigned_user_id")
            if assigned_user_id:
                fields["assignee"] = _get_user_label(conn, int(assigned_user_id))
            else:
                fields["assignee"] = None
        if fields:
            assignments = ", ".join([f"{key}=?" for key in fields])
            params = list(fields.values()) + [task_id]
            conn.execute(
                f"UPDATE tasks SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                params,
            )
        if comment:
            conn.execute(
                "INSERT INTO task_comments (task_id, comment, author) VALUES (?, ?, ?)",
                (task_id, comment, comment_author),
            )
        row = conn.execute("""
            SELECT t.*, u.display_name AS assignee_user_display_name, u.username AS assignee_user_username
            FROM tasks t
            LEFT JOIN users u ON u.id = t.assigned_user_id
            WHERE t.id=?
        """, (task_id,)).fetchone()
        return _decorate_task(row, conn) if row else None


def get_task(task_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT t.*, u.display_name AS assignee_user_display_name, u.username AS assignee_user_username
            FROM tasks t
            LEFT JOIN users u ON u.id = t.assigned_user_id
            WHERE t.id=?
        """, (task_id,)).fetchone()
        return _decorate_task(row, conn) if row else None


def list_tasks(status: str | None = None, bucket: str | None = None, assigned_user_id: int | None = None) -> list[dict[str, Any]]:
    clauses = ["c.marketplace != 'mock'"]
    params: list[Any] = []
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    elif bucket == "active":
        clauses.append("t.status NOT IN ('done', 'archived', 'cancelled')")
    elif bucket == "done":
        clauses.append("t.status = 'done'")
    elif bucket == "archive":
        clauses.append("t.status IN ('archived', 'cancelled')")
    if assigned_user_id:
        clauses.append("t.assigned_user_id = ?")
        params.append(int(assigned_user_id))
    where = f"WHERE {' AND '.join(clauses)}"
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                t.*,
                c.marketplace,
                c.customer_name,
                c.customer_public_id,
                c.order_id,
                c.external_chat_id,
                c.status AS chat_status,
                u.display_name AS assignee_user_display_name,
                u.username AS assignee_user_username
            FROM tasks t
            JOIN chats c ON c.id = t.chat_id
            LEFT JOIN users u ON u.id = t.assigned_user_id
            {where}
            ORDER BY
                CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'done' THEN 2 WHEN 'archived' THEN 3 ELSE 4 END,
                CASE WHEN t.due_at IS NULL OR t.due_at='' THEN 1 ELSE 0 END,
                datetime(t.due_at),
                datetime(t.updated_at) DESC
            """,
            params,
        ).fetchall()
        return [_decorate_task(r, conn) for r in rows]



def list_assignees() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, username, display_name, role, is_active
            FROM users
            WHERE is_active=1
            ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END, display_name COLLATE NOCASE, username COLLATE NOCASE
        """).fetchall()
        return [row_to_dict(r) for r in rows]


def list_knowledge_categories() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT kc.*, COUNT(ka.id) AS articles_count
            FROM knowledge_categories kc
            LEFT JOIN knowledge_articles ka ON ka.category_id = kc.id AND ka.is_published=1
            GROUP BY kc.id
            ORDER BY kc.sort_order ASC, kc.title COLLATE NOCASE ASC
        """).fetchall()
        return [row_to_dict(r) for r in rows]


def create_knowledge_category(title: str, description: str | None = None, sort_order: int = 0) -> dict[str, Any]:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO knowledge_categories (title, description, sort_order) VALUES (?, ?, ?)", (title.strip(), (description or '').strip() or None, int(sort_order or 0)))
        row = conn.execute("SELECT * FROM knowledge_categories WHERE id=?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)


def list_knowledge_articles(category_id: int | None = None, q: str | None = None) -> list[dict[str, Any]]:
    clauses = ["ka.is_published=1"]
    params: list[Any] = []
    if category_id:
        clauses.append("ka.category_id=?")
        params.append(int(category_id))
    if q and q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(ka.title LIKE ? OR ka.content LIKE ? OR COALESCE(ka.tags,'') LIKE ?)")
        params.extend([like, like, like])
    where = "WHERE " + " AND ".join(clauses)
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT ka.*, kc.title AS category_title, u.display_name AS updated_by_display_name, u.username AS updated_by_username
            FROM knowledge_articles ka
            LEFT JOIN knowledge_categories kc ON kc.id = ka.category_id
            LEFT JOIN users u ON u.id = ka.updated_by_user_id
            {where}
            ORDER BY datetime(ka.updated_at) DESC, ka.id DESC
        """, params).fetchall()
        return [row_to_dict(r) for r in rows]


def get_knowledge_article(article_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT ka.*, kc.title AS category_title, u.display_name AS updated_by_display_name, u.username AS updated_by_username
            FROM knowledge_articles ka
            LEFT JOIN knowledge_categories kc ON kc.id = ka.category_id
            LEFT JOIN users u ON u.id = ka.updated_by_user_id
            WHERE ka.id=?
        """, (article_id,)).fetchone()
        return row_to_dict(row) if row else None


def create_knowledge_article(*, category_id: int | None, title: str, content: str, tags: str | None, image_url: str | None = None, user_id: int | None, is_published: bool = True) -> dict[str, Any]:
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO knowledge_articles (category_id, title, content, tags, image_url, is_published, created_by_user_id, updated_by_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (category_id, title.strip(), content or '', (tags or '').strip() or None, (image_url or '').strip() or None, 1 if is_published else 0, user_id, user_id))
        return get_knowledge_article(int(cur.lastrowid)) or {}


def update_knowledge_article(article_id: int, *, category_id: int | None = None, title: str | None = None, content: str | None = None, tags: str | None = None, image_url: str | None = None, clear_image: bool = False, is_published: bool | None = None, user_id: int | None = None) -> dict[str, Any] | None:
    fields = []
    params: list[Any] = []
    if category_id is not None:
        fields.append("category_id=?"); params.append(category_id)
    if title is not None:
        fields.append("title=?"); params.append(title.strip())
    if content is not None:
        fields.append("content=?"); params.append(content)
    if tags is not None:
        fields.append("tags=?"); params.append(tags.strip() or None)
    if clear_image:
        fields.append("image_url=?"); params.append(None)
    elif image_url is not None:
        fields.append("image_url=?"); params.append(image_url.strip() or None)
    if is_published is not None:
        fields.append("is_published=?"); params.append(1 if is_published else 0)
    fields.append("updated_by_user_id=?"); params.append(user_id)
    fields.append("updated_at=CURRENT_TIMESTAMP")
    params.append(article_id)
    with get_connection() as conn:
        exists = conn.execute("SELECT id FROM knowledge_articles WHERE id=?", (article_id,)).fetchone()
        if not exists:
            return None
        conn.execute(f"UPDATE knowledge_articles SET {', '.join(fields)} WHERE id=?", params)
    return get_knowledge_article(article_id)

def log_webhook_event(source: str, event_type: str | None, external_id: str | None, payload: dict[str, Any]) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO webhook_events (source, event_type, external_id, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (source, event_type, external_id, json.dumps(payload, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def stats() -> dict[str, Any]:
    with get_connection() as conn:
        by_status = conn.execute("SELECT status, COUNT(*) AS count FROM chats WHERE marketplace != 'mock' GROUP BY status").fetchall()
        by_marketplace = conn.execute("SELECT marketplace, COUNT(*) AS count FROM chats WHERE marketplace != 'mock' GROUP BY marketplace").fetchall()
        tasks_open = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tasks t
            JOIN chats c ON c.id=t.chat_id
            WHERE t.status NOT IN ('done','archived','cancelled') AND c.marketplace != 'mock'
            """
        ).fetchone()
        waiting = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM chats c
            WHERE c.marketplace != 'mock' AND c.status != 'closed' AND (
                SELECT m.direction
                FROM messages m
                WHERE m.chat_id = c.id
                ORDER BY datetime(m.created_at) DESC, m.id DESC
                LIMIT 1
            ) = 'inbound'
            """
        ).fetchone()
        archived = conn.execute(
            "SELECT COUNT(*) AS count FROM chats WHERE marketplace != 'mock' AND status='closed'"
        ).fetchone()
        reviews_new = conn.execute(
            "SELECT COUNT(*) AS count FROM reviews WHERE marketplace='ozon' AND (reply_text IS NULL OR reply_text='')"
        ).fetchone()
        question_rows = conn.execute(
            """
            SELECT status
            FROM ozon_questions
            WHERE answer_text IS NULL OR answer_text=''
            """
        ).fetchall()
        questions_unanswered_count = sum(
            1 for row in question_rows
            if not _is_ozon_question_processed_status(row["status"] if row else None)
        )
        return {
            "chats_by_status": {r["status"]: r["count"] for r in by_status},
            "chats_by_marketplace": {r["marketplace"]: r["count"] for r in by_marketplace},
            "tasks_open": tasks_open["count"] if tasks_open else 0,
            "waiting_response": waiting["count"] if waiting else 0,
            "archived_chats": archived["count"] if archived else 0,
            "reviews_unanswered": reviews_new["count"] if reviews_new else 0,
            "questions_unanswered": questions_unanswered_count,
        }


def _review_row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    for key in ("raw_json", "media_json", "comments_json"):
        if key in data:
            public_key = key[:-5] if key.endswith("_json") else key
            try:
                data[public_key] = json.loads(data.pop(key) or ("[]" if key != "raw_json" else "{}"))
            except Exception:
                data[public_key] = [] if key != "raw_json" else {}
    return data


def upsert_review(review: dict[str, Any]) -> int:
    """Insert/update a normalized marketplace review."""
    marketplace = review.get("marketplace") or "ozon"
    external_review_id = str(review.get("external_review_id") or review.get("id") or "").strip()
    if not external_review_id:
        raise ValueError("external_review_id is required")
    media = review.get("media") or []
    comments = review.get("comments") or []
    raw = review.get("raw") or {}
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO reviews (
                marketplace, external_review_id, sku, product_name, rating, status,
                author_name, text, published_at, comments_amount, photos_amount, videos_amount,
                reply_text, reply_created_at, posting_number, media_json, comments_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marketplace, external_review_id) DO UPDATE SET
                sku=COALESCE(NULLIF(excluded.sku, ''), sku),
                product_name=COALESCE(NULLIF(excluded.product_name, ''), product_name),
                rating=COALESCE(excluded.rating, rating),
                status=COALESCE(NULLIF(excluded.status, ''), status),
                author_name=COALESCE(NULLIF(excluded.author_name, ''), author_name),
                text=COALESCE(excluded.text, text),
                published_at=COALESCE(NULLIF(excluded.published_at, ''), published_at),
                comments_amount=COALESCE(excluded.comments_amount, comments_amount),
                photos_amount=COALESCE(excluded.photos_amount, photos_amount),
                videos_amount=COALESCE(excluded.videos_amount, videos_amount),
                reply_text=COALESCE(NULLIF(excluded.reply_text, ''), reply_text),
                reply_created_at=COALESCE(NULLIF(excluded.reply_created_at, ''), reply_created_at),
                posting_number=COALESCE(NULLIF(excluded.posting_number, ''), posting_number),
                media_json=excluded.media_json,
                comments_json=excluded.comments_json,
                raw_json=excluded.raw_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                marketplace,
                external_review_id,
                str(review.get("sku") or ""),
                review.get("product_name"),
                review.get("rating"),
                review.get("status"),
                review.get("author_name"),
                review.get("text"),
                review.get("published_at"),
                int(review.get("comments_amount") or 0),
                int(review.get("photos_amount") or 0),
                int(review.get("videos_amount") or 0),
                review.get("reply_text"),
                review.get("reply_created_at"),
                review.get("posting_number"),
                json.dumps(media, ensure_ascii=False),
                json.dumps(comments, ensure_ascii=False),
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT id FROM reviews WHERE marketplace=? AND external_review_id=?",
            (marketplace, external_review_id),
        ).fetchone()
        return int(row["id"])


def list_reviews(marketplace: str | None = "ozon", status: str | None = None, unanswered: bool = False) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if marketplace:
        clauses.append("marketplace=?")
        params.append(marketplace)
    if status:
        clauses.append("status=?")
        params.append(status)
    if unanswered:
        clauses.append("(reply_text IS NULL OR reply_text='')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM reviews
            {where}
            ORDER BY datetime(COALESCE(published_at, updated_at, created_at)) DESC, id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        return [_review_row_to_dict(r) for r in rows]


def get_review(review_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return _review_row_to_dict(row) if row else None


def get_review_by_external(marketplace: str, external_review_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE marketplace=? AND external_review_id=?",
            (marketplace, external_review_id),
        ).fetchone()
        return _review_row_to_dict(row) if row else None


def mark_review_replied(review_id: int, reply_text: str, raw_response: dict[str, Any] | None = None, status: str | None = "PROCESSED") -> dict[str, Any] | None:
    raw_response = raw_response or {}
    current = get_review(review_id) or {}
    raw = current.get("raw") if isinstance(current.get("raw"), dict) else {}
    raw = {**raw, "_crm_reply_response": raw_response}
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE reviews SET
                reply_text=?,
                reply_created_at=CURRENT_TIMESTAMP,
                status=COALESCE(?, status),
                raw_json=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (reply_text, status, json.dumps(raw, ensure_ascii=False), review_id),
        )
    return get_review(review_id)


def link_review_chat(review_id: int, chat_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE reviews SET linked_chat_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (chat_id, review_id),
        )
    return get_review(review_id)


# -----------------------------
# Ozon questions / answers
# -----------------------------

def _question_row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    if "raw_json" in data:
        try:
            data["raw"] = json.loads(data.pop("raw_json") or "{}")
        except Exception:
            data["raw"] = {}
    is_processed = _is_ozon_question_processed_status(data.get("status"))
    has_answer = bool(str(data.get("answer_text") or "").strip())
    data["is_processed"] = is_processed
    data["needs_answer"] = (not has_answer) and (not is_processed)
    return data


def upsert_ozon_question(question: dict[str, Any]) -> int:
    """Insert/update a normalized Ozon product question."""
    external_question_id = str(
        question.get("external_question_id")
        or question.get("question_id")
        or question.get("id")
        or ""
    ).strip()
    if not external_question_id:
        raise ValueError("external_question_id is required")
    raw = question.get("raw") or {}
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ozon_questions (
                external_question_id, sku, product_name, product_url, status,
                author_name, text, published_at, answer_text, answer_created_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_question_id) DO UPDATE SET
                sku=COALESCE(NULLIF(excluded.sku, ''), sku),
                product_name=COALESCE(NULLIF(excluded.product_name, ''), product_name),
                product_url=COALESCE(NULLIF(excluded.product_url, ''), product_url),
                status=COALESCE(NULLIF(excluded.status, ''), status),
                author_name=COALESCE(NULLIF(excluded.author_name, ''), author_name),
                text=COALESCE(NULLIF(excluded.text, ''), text),
                published_at=COALESCE(NULLIF(excluded.published_at, ''), published_at),
                answer_text=COALESCE(NULLIF(excluded.answer_text, ''), answer_text),
                answer_created_at=COALESCE(NULLIF(excluded.answer_created_at, ''), answer_created_at),
                raw_json=excluded.raw_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                external_question_id,
                str(question.get("sku") or ""),
                question.get("product_name"),
                question.get("product_url"),
                question.get("status"),
                question.get("author_name"),
                question.get("text"),
                question.get("published_at"),
                question.get("answer_text"),
                question.get("answer_created_at"),
                json.dumps(raw, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT id FROM ozon_questions WHERE external_question_id=?",
            (external_question_id,),
        ).fetchone()
        return int(row["id"])


def list_ozon_questions(status: str | None = None, unanswered: bool = False) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if unanswered:
        clauses.append("(answer_text IS NULL OR answer_text='')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM ozon_questions
            {where}
            ORDER BY datetime(COALESCE(published_at, updated_at, created_at)) DESC, id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        items = [_question_row_to_dict(r) for r in rows]
        if unanswered:
            items = [item for item in items if item.get("needs_answer")]
        return items


def get_ozon_question(question_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM ozon_questions WHERE id=?", (question_id,)).fetchone()
        return _question_row_to_dict(row) if row else None


def get_ozon_question_by_external(external_question_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM ozon_questions WHERE external_question_id=?",
            (str(external_question_id),),
        ).fetchone()
        return _question_row_to_dict(row) if row else None


def mark_ozon_question_answered(
    question_id: int,
    answer_text: str,
    raw_response: dict[str, Any] | None = None,
    status: str | None = "PROCESSED",
) -> dict[str, Any] | None:
    raw_response = raw_response or {}
    current = get_ozon_question(question_id) or {}
    raw = current.get("raw") if isinstance(current.get("raw"), dict) else {}
    raw = {**raw, "_crm_answer_response": raw_response}
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE ozon_questions SET
                answer_text=?,
                answer_created_at=CURRENT_TIMESTAMP,
                status=COALESCE(?, status),
                raw_json=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (answer_text, status, json.dumps(raw, ensure_ascii=False), question_id),
        )
    return get_ozon_question(question_id)

# -----------------------------
# Users / authentication helpers
# -----------------------------
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone


def _hash_password(password: str, *, salt: bytes | None = None, iterations: int = 260_000) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return 'pbkdf2_sha256${}${}${}'.format(
        iterations,
        base64.b64encode(salt).decode('ascii'),
        base64.b64encode(dk).decode('ascii'),
    )


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_s, hash_s = stored.split('$', 3)
        if algo != 'pbkdf2_sha256':
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_s.encode('ascii'))
        expected = base64.b64decode(hash_s.encode('ascii'))
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def ensure_initial_admin(username: str, password: str, display_name: str | None = None) -> dict[str, Any] | None:
    """Create first admin user if the users table is empty."""
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if int(count or 0) > 0:
            return None
        password_hash = _hash_password(password)
        cur = conn.execute(
            """
            INSERT INTO users (username, display_name, role, password_hash, is_active)
            VALUES (?, ?, 'admin', ?, 1)
            """,
            (username.strip(), (display_name or username).strip(), password_hash),
        )
        row = conn.execute("SELECT id, username, display_name, role, is_active, created_at FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)


def create_user(username: str, password: str, display_name: str | None = None, role: str = 'manager') -> dict[str, Any]:
    role = role if role in {'admin', 'manager', 'viewer'} else 'manager'
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, display_name, role, password_hash, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (username.strip(), (display_name or username).strip(), role, _hash_password(password)),
        )
        row = conn.execute("SELECT id, username, display_name, role, is_active, created_at, updated_at FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)


def list_users() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, username, display_name, role, is_active, created_at, updated_at FROM users ORDER BY id ASC").fetchall()
        return [row_to_dict(r) for r in rows]


def update_user(user_id: int, *, display_name: str | None = None, role: str | None = None, is_active: bool | None = None) -> dict[str, Any] | None:
    fields=[]
    params=[]
    if display_name is not None:
        fields.append('display_name=?')
        params.append(display_name.strip() or None)
    if role is not None:
        if role not in {'admin','manager','viewer'}:
            role='manager'
        fields.append('role=?')
        params.append(role)
    if is_active is not None:
        fields.append('is_active=?')
        params.append(1 if is_active else 0)
    if not fields:
        return get_user_by_id(user_id)
    fields.append('updated_at=CURRENT_TIMESTAMP')
    params.append(user_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", params)
    return get_user_by_id(user_id)



def update_user_profile(user_id: int, *, username: str | None = None, display_name: str | None = None) -> dict[str, Any] | None:
    fields=[]
    params=[]
    if username is not None:
        username_clean = username.strip()
        if not username_clean:
            raise ValueError('Логин не может быть пустым')
        with get_connection() as conn:
            existing = conn.execute('SELECT id FROM users WHERE lower(username)=lower(?) AND id<>?', (username_clean, user_id)).fetchone()
            if existing:
                raise ValueError('Такой логин уже используется')
        fields.append('username=?')
        params.append(username_clean)
    if display_name is not None:
        fields.append('display_name=?')
        params.append((display_name or '').strip() or None)
    if not fields:
        return get_user_by_id(user_id)
    fields.append('updated_at=CURRENT_TIMESTAMP')
    params.append(user_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", params)
    return get_user_by_id(user_id)


def verify_user_password(user_id: int, password: str) -> bool:
    with get_connection() as conn:
        row = conn.execute('SELECT password_hash FROM users WHERE id=? AND is_active=1', (user_id,)).fetchone()
        if not row:
            return False
        return _verify_password(password or '', row['password_hash'] or '')

def update_user_password(user_id: int, password: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (_hash_password(password), user_id),
        )


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT id, username, display_name, role, is_active, created_at, updated_at FROM users WHERE id=?", (user_id,)).fetchone()
        return row_to_dict(row) if row else None


def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(username)=lower(?) AND is_active=1", (username.strip(),)).fetchone()
        if not row:
            return None
        data = dict(row)
        if not _verify_password(password, data.get('password_hash') or ''):
            return None
        return {k: data[k] for k in ('id', 'username', 'display_name', 'role', 'is_active', 'created_at', 'updated_at') if k in data}


def create_session(user_id: int, *, user_agent: str | None = None, ip: str | None = None, days: int = 14) -> str:
    token = secrets.token_urlsafe(48)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec='seconds')
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (session_token, user_id, expires_at, user_agent, ip) VALUES (?, ?, ?, ?, ?)",
            (token, user_id, expires_at, user_agent, ip),
        )
    return token


def get_user_by_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at, u.updated_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token=? AND s.revoked_at IS NULL AND s.expires_at > ? AND u.is_active=1
            """,
            (token, now),
        ).fetchone()
        return row_to_dict(row) if row else None


def revoke_session(token: str | None) -> None:
    if not token:
        return
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET revoked_at=CURRENT_TIMESTAMP WHERE session_token=?", (token,))


def cleanup_expired_sessions() -> int:
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL", (now,))
        return int(cur.rowcount or 0)



def _ensure_reply_templates_table(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reply_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by_user_id INTEGER,
            updated_by_user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reply_templates)").fetchall()}
    for column_name, column_sql in {
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "created_by_user_id": "INTEGER",
        "updated_by_user_id": "INTEGER",
        "updated_at": "TEXT",
    }.items():
        if column_name not in columns:
            conn.execute(f"ALTER TABLE reply_templates ADD COLUMN {column_name} {column_sql}")
    conn.execute("UPDATE reply_templates SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL OR updated_at=''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reply_templates_active_sort ON reply_templates(is_active, sort_order, updated_at DESC)")


def list_reply_templates(q: str | None = None, include_inactive: bool = False) -> list[dict[str, Any]]:
    params: list[Any] = []
    where: list[str] = []
    if not include_inactive:
        where.append('rt.is_active=1')
    if q:
        token = f"%{str(q).strip()}%"
        where.append('(rt.title LIKE ? OR rt.content LIKE ?)')
        params.extend([token, token])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with get_connection() as conn:
        _ensure_reply_templates_table(conn)
        rows = conn.execute(
            f"""
            SELECT rt.*, cu.username AS created_by_username, cu.display_name AS created_by_display_name,
                   uu.username AS updated_by_username, uu.display_name AS updated_by_display_name
            FROM reply_templates rt
            LEFT JOIN users cu ON cu.id = rt.created_by_user_id
            LEFT JOIN users uu ON uu.id = rt.updated_by_user_id
            {where_sql}
            ORDER BY rt.sort_order ASC, rt.updated_at DESC, rt.id DESC
            """,
            params,
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = row_to_dict(row)
        item['created_by'] = _user_label_from_row({
            'username': item.pop('created_by_username', None),
            'display_name': item.pop('created_by_display_name', None),
        })
        item['updated_by'] = _user_label_from_row({
            'username': item.pop('updated_by_username', None),
            'display_name': item.pop('updated_by_display_name', None),
        })
        item['is_active'] = bool(item.get('is_active', 1))
        items.append(item)
    return items


def create_reply_template(*, title: str, content: str, sort_order: int = 0, user_id: int | None = None) -> dict[str, Any]:
    title = (title or '').strip()
    content = str(content or '').strip()
    if not title:
        raise ValueError('Template title is required')
    if not content:
        raise ValueError('Template content is required')
    with get_connection() as conn:
        _ensure_reply_templates_table(conn)
        cur = conn.execute(
            """
            INSERT INTO reply_templates (title, content, sort_order, is_active, created_by_user_id, updated_by_user_id, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
            """,
            (title, content, int(sort_order or 0), user_id, user_id),
        )
        template_id = int(cur.lastrowid)
    return get_reply_template(template_id) or {}


def get_reply_template(template_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        _ensure_reply_templates_table(conn)
        row = conn.execute(
            """
            SELECT rt.*, cu.username AS created_by_username, cu.display_name AS created_by_display_name,
                   uu.username AS updated_by_username, uu.display_name AS updated_by_display_name
            FROM reply_templates rt
            LEFT JOIN users cu ON cu.id = rt.created_by_user_id
            LEFT JOIN users uu ON uu.id = rt.updated_by_user_id
            WHERE rt.id=?
            LIMIT 1
            """,
            (template_id,),
        ).fetchone()
    if not row:
        return None
    item = row_to_dict(row)
    item['created_by'] = _user_label_from_row({
        'username': item.pop('created_by_username', None),
        'display_name': item.pop('created_by_display_name', None),
    })
    item['updated_by'] = _user_label_from_row({
        'username': item.pop('updated_by_username', None),
        'display_name': item.pop('updated_by_display_name', None),
    })
    item['is_active'] = bool(item.get('is_active', 1))
    return item


def update_reply_template(template_id: int, *, title: str | None = None, content: str | None = None, sort_order: int | None = None, is_active: bool | None = None, user_id: int | None = None) -> dict[str, Any] | None:
    fields: list[str] = []
    params: list[Any] = []
    if title is not None:
        title = str(title).strip()
        if not title:
            raise ValueError('Template title is required')
        fields.append('title=?')
        params.append(title)
    if content is not None:
        content = str(content).strip()
        if not content:
            raise ValueError('Template content is required')
        fields.append('content=?')
        params.append(content)
    if sort_order is not None:
        fields.append('sort_order=?')
        params.append(int(sort_order))
    if is_active is not None:
        fields.append('is_active=?')
        params.append(1 if is_active else 0)
    if user_id is not None:
        fields.append('updated_by_user_id=?')
        params.append(user_id)
    if not fields:
        return get_reply_template(template_id)
    fields.append('updated_at=CURRENT_TIMESTAMP')
    params.append(template_id)
    with get_connection() as conn:
        _ensure_reply_templates_table(conn)
        exists = conn.execute('SELECT id FROM reply_templates WHERE id=?', (template_id,)).fetchone()
        if not exists:
            return None
        conn.execute(f"UPDATE reply_templates SET {', '.join(fields)} WHERE id=?", params)
    return get_reply_template(template_id)

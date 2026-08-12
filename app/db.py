from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "./crm.sqlite3")


_TECHNICAL_MESSAGE_AUTHORS = {
    "seller",
    "manager",
    "operator",
    "admin",
    "support",
    "employee",
    "staff",
    "merchant",
    "supplier",
    "vendor",
    "customer",
    "buyer",
    "client",
    "outbound",
    "продавец",
    "менеджер",
    "оператор",
    "администратор",
    "покупатель",
    "клиент",
    "мы",
}


def _json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on"}


def _clean_author_label(value: Any) -> str | None:
    label = str(value or "").strip()
    if not label or label.casefold() in _TECHNICAL_MESSAGE_AUTHORS:
        return None
    return label


def _raw_marks_crm_send(payload: dict[str, Any]) -> bool:
    return bool(
        _truthy(payload.get("_crm_sent_from_crm"))
        or payload.get("_crm_sent_by_label")
        or payload.get("_crm_sent_by_user_id")
        or payload.get("_crm_client_operation_id")
        or _truthy(payload.get("_crm_marketplace_attachment_sent"))
        or _truthy(payload.get("_crm_local_attachment"))
    )


def _merge_raw_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        if isinstance(payload, dict):
            merged.update(payload)
    return merged


def _message_row_priority(row: sqlite3.Row) -> tuple[int, int, int]:
    return (
        int(row["is_crm_sent"] or 0),
        1 if str(row["crm_author_label"] or "").strip() else 0,
        -int(row["id"]),
    )


def _merge_duplicate_message_rows(
    conn: sqlite3.Connection,
    canonical: sqlite3.Row,
    duplicate: sqlite3.Row,
    *,
    prefer_duplicate_external_id: bool = False,
) -> None:
    canonical_raw = _json_object(canonical["raw_json"])
    duplicate_raw = _json_object(duplicate["raw_json"])
    canonical_is_crm = bool(canonical["is_crm_sent"])
    duplicate_is_crm = bool(duplicate["is_crm_sent"])

    # Marketplace payload enriches the CRM row, while CRM provenance always wins.
    provider_raw = duplicate_raw if canonical_is_crm else canonical_raw
    crm_raw = canonical_raw if canonical_is_crm else duplicate_raw
    merged_raw = _merge_raw_payloads(provider_raw, crm_raw)

    canonical_external_id = str(canonical["external_message_id"] or "").strip()
    duplicate_external_id = str(duplicate["external_message_id"] or "").strip()
    external_message_id = canonical_external_id or duplicate_external_id or None
    if prefer_duplicate_external_id and duplicate_external_id:
        external_message_id = duplicate_external_id
        if canonical_external_id and canonical_external_id != duplicate_external_id:
            merged_raw.setdefault("_crm_send_ack_message_id", canonical_external_id)

    crm_author_label = (
        _clean_author_label(canonical["crm_author_label"])
        or _clean_author_label(duplicate["crm_author_label"])
        or _clean_author_label(crm_raw.get("_crm_sent_by_label"))
        or _clean_author_label(canonical["author"] if canonical_is_crm else duplicate["author"])
    )
    crm_author_user_id = canonical["crm_author_user_id"] or duplicate["crm_author_user_id"]
    client_operation_id = canonical["client_operation_id"] or duplicate["client_operation_id"]
    is_crm_sent = int(canonical_is_crm or duplicate_is_crm)
    direction = "outbound" if is_crm_sent else str(canonical["direction"] or duplicate["direction"])
    author = crm_author_label or canonical["author"] or duplicate["author"]

    created_at = canonical["created_at"]
    if prefer_duplicate_external_id and duplicate["created_at"]:
        created_at = duplicate["created_at"]

    # Remove the redundant row before assigning its unique provider identity to
    # the canonical row. All values needed for the merge are already in memory.
    conn.execute("DELETE FROM messages WHERE id=?", (int(duplicate["id"]),))
    conn.execute(
        """
        UPDATE messages
        SET external_message_id=?, direction=?, author=?, text=?, created_at=?, raw_json=?,
            is_crm_sent=?, crm_author_user_id=?, crm_author_label=?, client_operation_id=?
        WHERE id=?
        """,
        (
            external_message_id,
            direction,
            author,
            canonical["text"] or duplicate["text"] or "",
            created_at,
            json.dumps(merged_raw, ensure_ascii=False),
            is_crm_sent,
            crm_author_user_id,
            crm_author_label,
            client_operation_id,
            int(canonical["id"]),
        ),
    )


def _apply_message_identity_migration(conn: sqlite3.Connection) -> None:
    """Move CRM message identity out of raw_json and repair old duplicates once."""
    migration_name = "20260806_message_identity"
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    already_applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name=?",
        (migration_name,),
    ).fetchone()
    if already_applied:
        return

    affected_chat_ids: set[int] = set()

    rows = conn.execute(
        """
        SELECT id, chat_id, external_message_id, direction, author, text, created_at,
               raw_json, is_crm_sent, crm_author_user_id, crm_author_label,
               client_operation_id
        FROM messages
        ORDER BY id
        """
    ).fetchall()

    sender_user_ids: set[int] = set()
    for row in rows:
        raw = _json_object(row["raw_json"])
        try:
            sender_user_id = int(row["crm_author_user_id"] or raw.get("_crm_sent_by_user_id") or 0)
        except (TypeError, ValueError):
            sender_user_id = 0
        if sender_user_id > 0:
            sender_user_ids.add(sender_user_id)

    sender_labels: dict[int, str] = {}
    if sender_user_ids:
        placeholders = ",".join("?" for _ in sender_user_ids)
        user_rows = conn.execute(
            f"SELECT id, username, display_name FROM users WHERE id IN ({placeholders})",
            tuple(sorted(sender_user_ids)),
        ).fetchall()
        for user_row in user_rows:
            label = _clean_author_label(user_row["display_name"] or user_row["username"])
            if label:
                sender_labels[int(user_row["id"])] = label

    for row in rows:
        raw = _json_object(row["raw_json"])
        is_crm_sent = bool(row["is_crm_sent"]) or _raw_marks_crm_send(raw)
        if not is_crm_sent:
            continue
        try:
            raw_user_id = int(raw.get("_crm_sent_by_user_id") or 0) or None
        except (TypeError, ValueError):
            raw_user_id = None
        label = (
            _clean_author_label(row["crm_author_label"])
            or _clean_author_label(raw.get("_crm_sent_by_label"))
            or sender_labels.get(int(row["crm_author_user_id"] or raw_user_id or 0))
            or _clean_author_label(row["author"])
        )
        operation_id = str(
            row["client_operation_id"] or raw.get("_crm_client_operation_id") or ""
        ).strip() or None
        conn.execute(
            """
            UPDATE messages
            SET direction='outbound', is_crm_sent=1,
                crm_author_user_id=COALESCE(crm_author_user_id, ?),
                crm_author_label=COALESCE(NULLIF(crm_author_label, ''), ?),
                client_operation_id=COALESCE(NULLIF(client_operation_id, ''), ?),
                author=COALESCE(NULLIF(?, ''), author)
            WHERE id=?
            """,
            (raw_user_id, label, operation_id, label, int(row["id"])),
        )

    # Exact provider identity duplicates are always the same logical message.
    duplicate_external_ids = conn.execute(
        """
        SELECT chat_id, external_message_id
        FROM messages
        WHERE external_message_id IS NOT NULL AND TRIM(external_message_id)<>''
        GROUP BY chat_id, external_message_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in duplicate_external_ids:
        group_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE chat_id=? AND external_message_id=?
            ORDER BY id
            """,
            (group["chat_id"], group["external_message_id"]),
        ).fetchall()
        canonical = max(group_rows, key=_message_row_priority)
        for duplicate in group_rows:
            if int(duplicate["id"]) == int(canonical["id"]):
                continue
            affected_chat_ids.add(int(group["chat_id"]))
            _merge_duplicate_message_rows(conn, canonical, duplicate)
            canonical = conn.execute("SELECT * FROM messages WHERE id=?", (canonical["id"],)).fetchone()

    # Old send ACK ids and history ids can differ. Merge only an unambiguous,
    # near-in-time provider echo into the CRM row, then let the unique indexes
    # prevent future races.
    crm_rows = conn.execute(
        """
        SELECT * FROM messages
        WHERE is_crm_sent=1 AND direction='outbound' AND TRIM(text)<>''
        ORDER BY id
        """
    ).fetchall()
    for crm_row in crm_rows:
        candidates = conn.execute(
            """
            SELECT *, ABS(strftime('%s', created_at) - strftime('%s', ?)) AS time_delta
            FROM messages
            WHERE chat_id=? AND id<>? AND is_crm_sent=0 AND direction='outbound'
              AND TRIM(text)=TRIM(?)
              AND ABS(strftime('%s', created_at) - strftime('%s', ?)) <= 180
            ORDER BY time_delta ASC, id ASC
            LIMIT 2
            """,
            (crm_row["created_at"], crm_row["chat_id"], crm_row["id"], crm_row["text"], crm_row["created_at"]),
        ).fetchall()
        # Do not merge repeated identical replies by guesswork. Historical
        # repair is allowed only when one provider echo is unambiguous.
        if len(candidates) != 1:
            continue
        provider_row = candidates[0]
        affected_chat_ids.add(int(crm_row["chat_id"]))
        _merge_duplicate_message_rows(
            conn,
            crm_row,
            provider_row,
            prefer_duplicate_external_id=True,
        )

    # Operation retries are one logical CRM send.
    duplicate_operations = conn.execute(
        """
        SELECT chat_id, client_operation_id
        FROM messages
        WHERE client_operation_id IS NOT NULL AND TRIM(client_operation_id)<>''
        GROUP BY chat_id, client_operation_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for group in duplicate_operations:
        group_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE chat_id=? AND client_operation_id=?
            ORDER BY id
            """,
            (group["chat_id"], group["client_operation_id"]),
        ).fetchall()
        canonical = max(group_rows, key=_message_row_priority)
        for duplicate in group_rows:
            if int(duplicate["id"]) == int(canonical["id"]):
                continue
            affected_chat_ids.add(int(group["chat_id"]))
            _merge_duplicate_message_rows(conn, canonical, duplicate)
            canonical = conn.execute("SELECT * FROM messages WHERE id=?", (canonical["id"],)).fetchone()

    for chat_id in affected_chat_ids:
        latest = conn.execute(
            """
            SELECT text, created_at
            FROM messages
            WHERE chat_id=?
            ORDER BY julianday(created_at) DESC, id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE chats
            SET last_message_preview=?, last_message_at=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                (latest["text"] or "")[:200] if latest else None,
                latest["created_at"] if latest else None,
                chat_id,
            ),
        )

    conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (migration_name,))


def _resolve_db_path() -> str:
    path = Path(DATABASE_PATH)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_resolve_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                customer_name TEXT,
                customer_public_id TEXT,
                order_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                assigned_to TEXT,
                assigned_user_id INTEGER,
                last_message_at TEXT,
                last_message_preview TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(marketplace, external_chat_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                external_message_id TEXT,
                direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound', 'internal')),
                author TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                raw_json TEXT NOT NULL DEFAULT '{}',
                is_crm_sent INTEGER NOT NULL DEFAULT 0 CHECK(is_crm_sent IN (0, 1)),
                crm_author_user_id INTEGER,
                crm_author_label TEXT,
                client_operation_id TEXT,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(crm_author_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS task_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                comment_label TEXT NOT NULL DEFAULT 'Комментарий',
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                task_type_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                assignee TEXT,
                assigned_user_id INTEGER,
                due_at TEXT,
                completed_at TEXT,
                archived_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                comment TEXT NOT NULL,
                author TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                chat_id INTEGER,
                task_id INTEGER,
                entity_type TEXT,
                entity_id TEXT,
                dedupe_key TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                user_agent TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                failure_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_success_at TEXT,
                last_error_at TEXT,
                last_error TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS push_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER,
                user_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                sent_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );



            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'manager',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                user_agent TEXT,
                ip TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_user_states (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                last_read_message_id INTEGER,
                last_read_at TEXT,
                is_marked_unread INTEGER NOT NULL DEFAULT 0 CHECK(is_marked_unread IN (0, 1)),
                is_pinned INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0, 1)),
                pinned_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, chat_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS yandex_oauth_links (
                yandex_user_id TEXT PRIMARY KEY,
                crm_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(crm_user_id) REFERENCES users(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_yandex_oauth_links_crm_user_id
            ON yandex_oauth_links(crm_user_id);

            CREATE TABLE IF NOT EXISTS yandex_oauth_managed_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier_type TEXT NOT NULL CHECK(identifier_type IN ('login', 'email')),
                display_identifier TEXT NOT NULL,
                normalized_identifier TEXT NOT NULL,
                crm_user_id INTEGER NOT NULL,
                yandex_user_id TEXT UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(identifier_type, normalized_identifier),
                FOREIGN KEY(crm_user_id) REFERENCES users(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_yandex_oauth_managed_links_crm_user_id
            ON yandex_oauth_managed_links(crm_user_id);

            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                event_type TEXT,
                external_id TEXT,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL DEFAULT 'ozon',
                external_review_id TEXT NOT NULL,
                sku TEXT,
                product_name TEXT,
                rating INTEGER,
                status TEXT,
                author_name TEXT,
                text TEXT,
                published_at TEXT,
                comments_amount INTEGER DEFAULT 0,
                photos_amount INTEGER DEFAULT 0,
                videos_amount INTEGER DEFAULT 0,
                reply_text TEXT,
                reply_created_at TEXT,
                posting_number TEXT,
                linked_chat_id INTEGER,
                media_json TEXT NOT NULL DEFAULT '[]',
                comments_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(marketplace, external_review_id),
                FOREIGN KEY(linked_chat_id) REFERENCES chats(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS ozon_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_question_id TEXT NOT NULL UNIQUE,
                sku TEXT,
                product_name TEXT,
                product_url TEXT,
                status TEXT,
                author_name TEXT,
                text TEXT,
                published_at TEXT,
                answer_text TEXT,
                answer_created_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Lightweight migrations for existing local SQLite databases.
        def _columns(table: str) -> set[str]:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

        task_columns = _columns("tasks")
        if "completed_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        if "archived_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
        if "task_type_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN task_type_id INTEGER")

        task_type_columns = _columns("task_types")
        if "comment_label" not in task_type_columns:
            conn.execute("ALTER TABLE task_types ADD COLUMN comment_label TEXT NOT NULL DEFAULT 'Комментарий'")
        if "sort_order" not in task_type_columns:
            conn.execute("ALTER TABLE task_types ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        if "is_active" not in task_type_columns:
            conn.execute("ALTER TABLE task_types ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        existing_task_types = conn.execute("SELECT COUNT(*) AS c FROM task_types").fetchone()["c"]
        if not existing_task_types:
            conn.execute(
                "INSERT INTO task_types (title, comment_label, sort_order, is_active) VALUES (?, ?, ?, 1)",
                ("Общая", "Комментарий", 0),
            )


        chat_columns = _columns("chats")
        if "assigned_user_id" not in chat_columns:
            conn.execute("ALTER TABLE chats ADD COLUMN assigned_user_id INTEGER")
        if "assigned_user_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN assigned_user_id INTEGER")

        message_columns = _columns("messages")
        if "is_crm_sent" not in message_columns:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN is_crm_sent INTEGER NOT NULL DEFAULT 0 CHECK(is_crm_sent IN (0, 1))"
            )
        if "crm_author_user_id" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN crm_author_user_id INTEGER")
        if "crm_author_label" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN crm_author_label TEXT")
        if "client_operation_id" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN client_operation_id TEXT")

        _apply_message_identity_migration(conn)

        chat_user_state_columns = _columns("chat_user_states")
        if "is_pinned" not in chat_user_state_columns:
            conn.execute(
                "ALTER TABLE chat_user_states ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0, 1))"
            )
        if "pinned_at" not in chat_user_state_columns:
            conn.execute("ALTER TABLE chat_user_states ADD COLUMN pinned_at TEXT")

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
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS knowledge_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tags TEXT,
                image_url TEXT,
                is_published INTEGER NOT NULL DEFAULT 1,
                created_by_user_id INTEGER,
                updated_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(category_id) REFERENCES knowledge_categories(id) ON DELETE SET NULL,
                FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )

        reply_template_columns = _columns("reply_templates")
        for column_name, column_sql in {
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "created_by_user_id": "INTEGER",
            "updated_by_user_id": "INTEGER",
            "updated_at": "TEXT",
        }.items():
            if column_name not in reply_template_columns:
                conn.execute(f"ALTER TABLE reply_templates ADD COLUMN {column_name} {column_sql}")
        conn.execute("UPDATE reply_templates SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL OR updated_at=''")

        knowledge_article_columns = _columns("knowledge_articles")
        if "image_url" not in knowledge_article_columns:
            conn.execute("ALTER TABLE knowledge_articles ADD COLUMN image_url TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ozon_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_question_id TEXT NOT NULL UNIQUE,
                sku TEXT,
                product_name TEXT,
                product_url TEXT,
                status TEXT,
                author_name TEXT,
                text TEXT,
                published_at TEXT,
                answer_text TEXT,
                answer_created_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        question_columns = _columns("ozon_questions")
        for column_name, column_sql in {
            "product_url": "TEXT",
            "answer_text": "TEXT",
            "answer_created_at": "TEXT",
            "raw_json": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if column_name not in question_columns:
                conn.execute(f"ALTER TABLE ozon_questions ADD COLUMN {column_name} {column_sql}")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_funnels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chat_statuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                funnel_id INTEGER,
                color TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_system INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(funnel_id) REFERENCES chat_funnels(id) ON DELETE SET NULL
            );
            """
        )

        funnel_columns = _columns("chat_funnels")
        for column_name, column_sql in {
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "is_default": "INTEGER NOT NULL DEFAULT 0",
            "updated_at": "TEXT",
        }.items():
            if column_name not in funnel_columns:
                conn.execute(f"ALTER TABLE chat_funnels ADD COLUMN {column_name} {column_sql}")

        status_columns = _columns("chat_statuses")
        for column_name, column_sql in {
            "funnel_id": "INTEGER",
            "color": "TEXT",
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "is_system": "INTEGER NOT NULL DEFAULT 0",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "updated_at": "TEXT",
        }.items():
            if column_name not in status_columns:
                conn.execute(f"ALTER TABLE chat_statuses ADD COLUMN {column_name} {column_sql}")

        default_funnel = conn.execute("SELECT id FROM chat_funnels WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()
        if not default_funnel:
            cur = conn.execute(
                "INSERT INTO chat_funnels (title, sort_order, is_default) VALUES (?, ?, 1)",
                ("Основная воронка", 0),
            )
            default_funnel_id = int(cur.lastrowid)
        else:
            default_funnel_id = int(default_funnel["id"])

        for key, title, color, sort_order in (
            ("new", "Новый", "orange", 10),
            ("in_progress", "В работе", "blue", 20),
            ("waiting_customer", "Ждём клиента", "purple", 30),
            ("closed", "Закрыт", "gray", 999),
        ):
            conn.execute(
                """
                INSERT INTO chat_statuses (key, title, funnel_id, color, sort_order, is_system, is_active)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                ON CONFLICT(key) DO UPDATE SET
                    title=excluded.title,
                    funnel_id=COALESCE(chat_statuses.funnel_id, excluded.funnel_id),
                    color=COALESCE(chat_statuses.color, excluded.color),
                    sort_order=CASE WHEN chat_statuses.sort_order=0 THEN excluded.sort_order ELSE chat_statuses.sort_order END,
                    is_system=1,
                    is_active=1,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (key, title, default_funnel_id, color, sort_order),
            )

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                comment TEXT NOT NULL,
                author TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                chat_id INTEGER,
                task_id INTEGER,
                entity_type TEXT,
                entity_id TEXT,
                dedupe_key TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );
            """
        )

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                user_agent TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                failure_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_success_at TEXT,
                last_error_at TEXT,
                last_error TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS push_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER,
                user_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                sent_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

        # Analytics and large-history indexes.
        # These are safe on existing SQLite databases and make daily/hourly
        # dashboards faster when messages grow to tens of thousands.
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_direction_created_at
                ON messages(direction, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_direction_created_at
                ON messages(chat_id, direction, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_created_id
                ON messages(chat_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_direction_id
                ON messages(chat_id, direction, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_external_unique
                ON messages(chat_id, external_message_id)
                WHERE external_message_id IS NOT NULL AND TRIM(external_message_id)<>'';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_operation_unique
                ON messages(chat_id, client_operation_id)
                WHERE client_operation_id IS NOT NULL AND TRIM(client_operation_id)<>'';
            CREATE INDEX IF NOT EXISTS idx_chat_user_states_chat_user
                ON chat_user_states(chat_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_chat_user_states_user_pinned
                ON chat_user_states(user_id, is_pinned, chat_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_chat_created_id
                ON tasks(chat_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_chats_marketplace_status_last_message
                ON chats(marketplace, status, last_message_at);
            CREATE INDEX IF NOT EXISTS idx_chats_marketplace_last_message
                ON chats(marketplace, last_message_at);
            CREATE INDEX IF NOT EXISTS idx_notifications_user_read_created
                ON notifications(user_id, is_read, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_notifications_chat
                ON notifications(chat_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_task
                ON notifications(task_id);
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_active
                ON push_subscriptions(user_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_endpoint
                ON push_subscriptions(endpoint);
            CREATE INDEX IF NOT EXISTS idx_push_outbox_pending
                ON push_outbox(sent_at, next_attempt_at, attempts, created_at);
            CREATE INDEX IF NOT EXISTS idx_push_outbox_user
                ON push_outbox(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_type
                ON tasks(status, task_type_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_due_at_id
                ON tasks(due_at, id);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigned_due_id
                ON tasks(assigned_user_id, due_at, id);
            CREATE INDEX IF NOT EXISTS idx_task_types_active_sort
                ON task_types(is_active, sort_order, title);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe
                ON notifications(dedupe_key) WHERE dedupe_key IS NOT NULL AND dedupe_key != '';
            CREATE INDEX IF NOT EXISTS idx_reply_templates_active_sort
                ON reply_templates(is_active, sort_order, updated_at DESC);
            """
        )


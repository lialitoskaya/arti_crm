from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "./crm.sqlite3")


def _resolve_db_path() -> str:
    path = Path(DATABASE_PATH)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_resolve_db_path())
    conn.row_factory = sqlite3.Row
    try:
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
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                assignee TEXT,
                assigned_user_id INTEGER,
                due_at TEXT,
                completed_at TEXT,
                archived_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
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


        chat_columns = _columns("chats")
        if "assigned_user_id" not in chat_columns:
            conn.execute("ALTER TABLE chats ADD COLUMN assigned_user_id INTEGER")
        if "assigned_user_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN assigned_user_id INTEGER")

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

        # Analytics and large-history indexes.
        # These are safe on existing SQLite databases and make daily/hourly
        # dashboards faster when messages grow to tens of thousands.
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_direction_created_at
                ON messages(direction, created_at);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_direction_created_at
                ON messages(chat_id, direction, created_at);
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe
                ON notifications(dedupe_key) WHERE dedupe_key IS NOT NULL AND dedupe_key != '';
            CREATE INDEX IF NOT EXISTS idx_reply_templates_active_sort
                ON reply_templates(is_active, sort_order, updated_at DESC);
            """
        )


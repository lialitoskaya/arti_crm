from __future__ import annotations

import sqlite3
from typing import Any

from app.db import get_connection, row_to_dict
from app.schemas import TaskTypeCreate, TaskTypeUpdate
from app.task_chat_status_automation import release_task_type_effects_conn, set_task_type_mapping_conn


_SELECT_TASK_TYPE_BY_ID = """
SELECT tt.*, l.chat_status_id, s.key AS chat_status_key,
       s.title AS chat_status_title
FROM task_types tt
LEFT JOIN task_type_chat_status_links l ON l.task_type_id=tt.id
LEFT JOIN chat_statuses s ON s.id=l.chat_status_id
WHERE tt.id=?
"""


def ensure_task_types_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            comment_label TEXT NOT NULL DEFAULT 'Комментарий',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_types)").fetchall()}
    if "comment_label" not in columns:
        conn.execute("ALTER TABLE task_types ADD COLUMN comment_label TEXT NOT NULL DEFAULT 'Комментарий'")
    if "sort_order" not in columns:
        conn.execute("ALTER TABLE task_types ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
    if "is_active" not in columns:
        conn.execute("ALTER TABLE task_types ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "task_type_id" not in task_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN task_type_id INTEGER")
    count = conn.execute("SELECT COUNT(*) AS c FROM task_types").fetchone()["c"]
    if not count:
        conn.execute(
            "INSERT INTO task_types (title, comment_label, sort_order, is_active) VALUES (?, ?, ?, 1)",
            ("Общая", "Комментарий", 0),
        )


def list_task_types(include_inactive: bool = False) -> list[dict[str, Any]]:
    with get_connection() as conn:
        ensure_task_types_table(conn)
        where = "" if include_inactive else "WHERE tt.is_active=1"
        rows = conn.execute(
            f"""
            SELECT tt.id, tt.title, tt.comment_label, tt.sort_order, tt.is_active,
                   tt.created_at, tt.updated_at, l.chat_status_id,
                   s.key AS chat_status_key, s.title AS chat_status_title
            FROM task_types tt
            LEFT JOIN task_type_chat_status_links l ON l.task_type_id=tt.id
            LEFT JOIN chat_statuses s ON s.id=l.chat_status_id
            {where}
            ORDER BY tt.is_active DESC, tt.sort_order ASC,
                     tt.title COLLATE NOCASE ASC, tt.id ASC
            """
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def create_task_type(payload: TaskTypeCreate) -> dict[str, Any]:
    title = (payload.title or "").strip()
    comment_label = (payload.comment_label or "Комментарий").strip() or "Комментарий"
    if not title:
        raise ValueError("Название типа задачи обязательно")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_task_types_table(conn)
        cur = conn.execute(
            """
            INSERT INTO task_types (title, comment_label, sort_order, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (title, comment_label, int(payload.sort_order or 0)),
        )
        task_type_id = int(cur.lastrowid)
        set_task_type_mapping_conn(conn, task_type_id, payload.chat_status_id)
        row = conn.execute(
            _SELECT_TASK_TYPE_BY_ID,
            (task_type_id,),
        ).fetchone()
        return row_to_dict(row)


def update_task_type(type_id: int, payload: TaskTypeUpdate) -> dict[str, Any] | None:
    fields = payload.model_dump(exclude_unset=True)
    mapping_was_set = "chat_status_id" in fields
    chat_status_id = fields.pop("chat_status_id", None)
    allowed = {"title", "comment_label", "sort_order", "is_active"}
    updates: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"title", "comment_label"} and value is not None:
            value = str(value).strip()
            if not value:
                continue
        if key == "is_active" and value is not None:
            value = 1 if value else 0
        updates[key] = value
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_task_types_table(conn)
        preimage = conn.execute(
            "SELECT id, is_active FROM task_types WHERE id=?", (int(type_id),)
        ).fetchone()
        if not preimage:
            return None
        if updates:
            assignments = ", ".join([f"{key}=?" for key in updates])
            cursor = conn.execute(
                f"""
                UPDATE task_types
                SET {assignments}, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND is_active=?
                """,
                list(updates.values()) + [int(type_id), int(preimage["is_active"] or 0)],
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Task type update lost its locked preimage")
        if mapping_was_set:
            set_task_type_mapping_conn(conn, int(type_id), chat_status_id)
        next_active = int(updates.get("is_active", preimage["is_active"]) or 0)
        if next_active == 0:
            release_task_type_effects_conn(conn, int(type_id))
        row = conn.execute(
            _SELECT_TASK_TYPE_BY_ID,
            (type_id,),
        ).fetchone()
        return row_to_dict(row) if row else None


def delete_task_type(type_id: int) -> bool:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_task_types_table(conn)
        row = conn.execute(
            "SELECT id, is_active FROM task_types WHERE id=?", (int(type_id),)
        ).fetchone()
        if not row:
            return False
        in_use = conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE task_type_id=?", (type_id,)).fetchone()["c"]
        if int(in_use or 0) > 0:
            cursor = conn.execute(
                """
                UPDATE task_types SET is_active=0, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND is_active=?
                """,
                (int(type_id), int(row["is_active"] or 0)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Task type deactivate lost its locked preimage")
            release_task_type_effects_conn(conn, int(type_id))
        else:
            cursor = conn.execute(
                "DELETE FROM task_types WHERE id=? AND is_active=?",
                (int(type_id), int(row["is_active"] or 0)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Task type delete lost its locked preimage")
        return True

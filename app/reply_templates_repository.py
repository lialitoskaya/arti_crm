from __future__ import annotations

import sqlite3
from typing import Any

from app.db import get_connection, row_to_dict, user_label_from_row


_SELECT_TEMPLATE_WITH_AUTHORS = """
SELECT rt.*, cu.username AS created_by_username, cu.display_name AS created_by_display_name,
       uu.username AS updated_by_username, uu.display_name AS updated_by_display_name
FROM reply_templates rt
LEFT JOIN users cu ON cu.id = rt.created_by_user_id
LEFT JOIN users uu ON uu.id = rt.updated_by_user_id
"""


def _template_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = row_to_dict(row)
    item['created_by'] = user_label_from_row({
        'username': item.pop('created_by_username', None),
        'display_name': item.pop('created_by_display_name', None),
    })
    item['updated_by'] = user_label_from_row({
        'username': item.pop('updated_by_username', None),
        'display_name': item.pop('updated_by_display_name', None),
    })
    item['is_active'] = bool(item.get('is_active', 1))
    return item


def _ensure_reply_templates_table(conn: sqlite3.Connection) -> None:
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
            {_SELECT_TEMPLATE_WITH_AUTHORS}
            {where_sql}
            ORDER BY rt.sort_order ASC, rt.updated_at DESC, rt.id DESC
            """,
            params,
        ).fetchall()
    return [_template_from_row(row) for row in rows]


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
            f"""
            {_SELECT_TEMPLATE_WITH_AUTHORS}
            WHERE rt.id=?
            LIMIT 1
            """,
            (template_id,),
        ).fetchone()
    return _template_from_row(row) if row else None


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


def delete_reply_template(template_id: int) -> bool:
    with get_connection() as conn:
        _ensure_reply_templates_table(conn)
        cursor = conn.execute("DELETE FROM reply_templates WHERE id=?", (template_id,))
        return cursor.rowcount > 0

from __future__ import annotations

import json
import sqlite3
from typing import Any


MIGRATION_NAME = "20260824_task_type_chat_status_automation"
TERMINAL_TASK_STATUSES = frozenset({"done", "archived", "cancelled"})
OVERRIDE_KINDS = frozenset({"manual", "provider", "rollback_release"})
_AUTOMATION_TABLES = (
    "task_chat_status_effects",
    "chat_task_status_state",
    "task_type_chat_status_links",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {str(row["name"]): row for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalize_default(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while len(normalized) >= 2 and normalized[0] == "(" and normalized[-1] == ")":
        normalized = normalized[1:-1].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1]
    return normalized.upper()


def _assert_column(
    columns: dict[str, sqlite3.Row],
    table: str,
    name: str,
    *,
    declared_type: str,
    not_null: bool | None = None,
    primary_key: bool | None = None,
    default: str | None | object = ...,
) -> None:
    row = columns.get(name)
    if row is None:
        raise RuntimeError(f"Incompatible {table} schema: missing column {name}")
    if str(row["type"] or "").upper() != declared_type:
        raise RuntimeError(f"Incompatible {table}.{name} type")
    if not_null is not None and bool(row["notnull"]) != not_null:
        raise RuntimeError(f"Incompatible {table}.{name} nullability")
    if primary_key is not None and bool(row["pk"]) != primary_key:
        raise RuntimeError(f"Incompatible {table}.{name} primary key")
    if default is not ... and _normalize_default(row["dflt_value"]) != _normalize_default(default):
        raise RuntimeError(f"Incompatible {table}.{name} default")


def _foreign_keys(
    conn: sqlite3.Connection, table: str
) -> set[tuple[str, str, str, str, str]]:
    return {
        (
            str(row["from"]),
            str(row["table"]),
            str(row["to"]),
            str(row["on_update"]).upper(),
            str(row["on_delete"]).upper(),
        )
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    }


def _unique_constraint_columns(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if not bool(row["unique"]) or str(row["origin"]) != "u":
            continue
        name = str(row["name"])
        columns = tuple(str(item["name"]) for item in conn.execute(f"PRAGMA index_info({name})"))
        result.add(columns)
    return result


def _normalized_table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return "".join(str(row["sql"] or "").lower().split()) if row else ""


def _assert_exact_columns(
    conn: sqlite3.Connection,
    table: str,
    expected: dict[str, tuple[str, bool, bool, str | None]],
    *,
    allow_missing_audit: set[str] | None = None,
) -> set[str]:
    columns = _table_columns(conn, table)
    missing = set(expected) - set(columns)
    allowed_missing = allow_missing_audit or set()
    unexpected_missing = missing - allowed_missing
    if unexpected_missing:
        raise RuntimeError(
            f"Incompatible {table} schema: missing columns {sorted(unexpected_missing)}"
        )
    extra = set(columns) - set(expected)
    if extra:
        raise RuntimeError(f"Incompatible {table} schema: unexpected columns {sorted(extra)}")
    for name, (declared_type, not_null, primary_key, default) in expected.items():
        if name in missing:
            continue
        _assert_column(
            columns,
            table,
            name,
            declared_type=declared_type,
            not_null=not_null,
            primary_key=primary_key,
            default=default,
        )
    return missing


_MAPPING_COLUMNS = {
    "task_type_id": ("INTEGER", False, True, None),
    "chat_status_id": ("INTEGER", True, False, None),
    "created_at": ("TEXT", True, False, "CURRENT_TIMESTAMP"),
    "updated_at": ("TEXT", True, False, "CURRENT_TIMESTAMP"),
}
_STATE_COLUMNS = {
    "chat_id": ("INTEGER", False, True, None),
    "cycle": ("INTEGER", True, False, "0"),
    "baseline_status_key": ("TEXT", False, False, None),
    "override_kind": ("TEXT", False, False, None),
    "active_task_id": ("INTEGER", False, False, None),
    "active_status_key": ("TEXT", False, False, None),
    "updated_at": ("TEXT", True, False, "CURRENT_TIMESTAMP"),
}
_EFFECT_COLUMNS = {
    "id": ("INTEGER", False, True, None),
    "task_id": ("INTEGER", True, False, None),
    "chat_id": ("INTEGER", True, False, None),
    "cycle": ("INTEGER", True, False, None),
    "chat_status_id": ("INTEGER", False, False, None),
    "mapped_status_key": ("TEXT", True, False, None),
    "applied_at": ("TEXT", True, False, "CURRENT_TIMESTAMP"),
    "released_at": ("TEXT", False, False, None),
    "release_reason": ("TEXT", False, False, None),
}


def _validate_schema_migrations(conn: sqlite3.Connection) -> None:
    _assert_exact_columns(
        conn,
        "schema_migrations",
        {
            "name": ("TEXT", False, True, None),
            "applied_at": ("TEXT", True, False, "CURRENT_TIMESTAMP"),
        },
    )


def _validate_table_contracts(
    conn: sqlite3.Connection, *, allow_missing_audit: bool = False
) -> set[tuple[str, str]]:
    missing: set[tuple[str, str]] = set()
    specifications = (
        ("task_type_chat_status_links", _MAPPING_COLUMNS, {"created_at", "updated_at"}),
        ("chat_task_status_state", _STATE_COLUMNS, {"updated_at"}),
        (
            "task_chat_status_effects",
            _EFFECT_COLUMNS,
            {"applied_at", "released_at", "release_reason"},
        ),
    )
    for table, expected, audit in specifications:
        if not _table_exists(conn, table):
            continue
        table_missing = _assert_exact_columns(
            conn,
            table,
            expected,
            allow_missing_audit=audit if allow_missing_audit else set(),
        )
        missing.update((table, name) for name in table_missing)
    return missing


def _create_automation_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_type_chat_status_links (
            task_type_id INTEGER PRIMARY KEY,
            chat_status_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON UPDATE NO ACTION ON DELETE CASCADE,
            FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON UPDATE NO ACTION ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_task_status_state (
            chat_id INTEGER PRIMARY KEY,
            cycle INTEGER NOT NULL DEFAULT 0 CHECK(cycle >= 0),
            baseline_status_key TEXT,
            override_kind TEXT CHECK(override_kind IN ('manual', 'provider', 'rollback_release')),
            active_task_id INTEGER,
            active_status_key TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chat_id) REFERENCES chats(id) ON UPDATE NO ACTION ON DELETE CASCADE,
            FOREIGN KEY(active_task_id) REFERENCES tasks(id) ON UPDATE NO ACTION ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_chat_status_effects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            cycle INTEGER NOT NULL CHECK(cycle > 0),
            chat_status_id INTEGER,
            mapped_status_key TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            released_at TEXT,
            release_reason TEXT,
            UNIQUE(task_id, cycle),
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON UPDATE NO ACTION ON DELETE CASCADE,
            FOREIGN KEY(chat_id) REFERENCES chats(id) ON UPDATE NO ACTION ON DELETE CASCADE,
            FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON UPDATE NO ACTION ON DELETE SET NULL
        )
        """
    )


def _index_contract(conn: sqlite3.Connection, table: str, name: str) -> tuple[bool, tuple[str, ...], bool, str]:
    row = next(
        (item for item in conn.execute(f"PRAGMA index_list({table})") if str(item["name"]) == name),
        None,
    )
    if row is None:
        raise RuntimeError(f"Incompatible {table} schema: missing index {name}")
    columns = tuple(str(item["name"]) for item in conn.execute(f"PRAGMA index_info({name})"))
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    normalized_sql = "".join(str(sql_row["sql"] or "").lower().split()) if sql_row else ""
    return bool(row["unique"]), columns, bool(row["partial"]), normalized_sql


_EXPECTED_INDEXES = (
    (
        "task_chat_status_effects",
        "idx_task_chat_effects_chat_cycle_active",
        False,
        ("chat_id", "cycle", "released_at", "applied_at", "id"),
        False,
        "",
    ),
    (
        "task_chat_status_effects",
        "idx_task_chat_effects_one_active_task",
        True,
        ("task_id",),
        True,
        "wherereleased_atisnull",
    ),
    (
        "task_type_chat_status_links",
        "idx_task_type_chat_status_links_status",
        False,
        ("chat_status_id",),
        False,
        "",
    ),
)


def _assert_index_definition(
    conn: sqlite3.Connection,
    table: str,
    name: str,
    unique: bool,
    columns: tuple[str, ...],
    partial: bool,
    predicate: str,
) -> None:
    actual_unique, actual_columns, actual_partial, sql = _index_contract(conn, table, name)
    if (actual_unique, actual_columns, actual_partial) != (unique, columns, partial):
        raise RuntimeError(f"Incompatible {name} definition")
    if predicate and predicate not in sql:
        raise RuntimeError(f"Incompatible {name} predicate")


def _validate_existing_index_contracts(conn: sqlite3.Connection) -> None:
    """Reject malformed named indexes before rebuilding an empty partial schema."""
    for table, name, unique, columns, partial, predicate in _EXPECTED_INDEXES:
        if not _table_exists(conn, table):
            continue
        names = {str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")}
        if name in names:
            _assert_index_definition(conn, table, name, unique, columns, partial, predicate)

    if _table_exists(conn, "task_chat_status_effects"):
        custom_unique_indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(task_chat_status_effects)")
            if bool(row["unique"]) and str(row["origin"]) == "c"
        }
        if not custom_unique_indexes.issubset({"idx_task_chat_effects_one_active_task"}):
            raise RuntimeError("Incompatible task_chat_status_effects unique indexes")


def _validate_existing_structural_contracts(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "task_type_chat_status_links") and _foreign_keys(
        conn, "task_type_chat_status_links"
    ) != {
        ("task_type_id", "task_types", "id", "NO ACTION", "CASCADE"),
        ("chat_status_id", "chat_statuses", "id", "NO ACTION", "RESTRICT"),
    }:
        raise RuntimeError("Incompatible task_type_chat_status_links foreign keys")
    if _table_exists(conn, "chat_task_status_state"):
        if _foreign_keys(conn, "chat_task_status_state") != {
            ("chat_id", "chats", "id", "NO ACTION", "CASCADE"),
            ("active_task_id", "tasks", "id", "NO ACTION", "SET NULL"),
        }:
            raise RuntimeError("Incompatible chat_task_status_state foreign keys")
        state_sql = _normalized_table_sql(conn, "chat_task_status_state")
        if (
            "check(cycle>=0)" not in state_sql
            or "check(override_kindin('manual','provider','rollback_release'))" not in state_sql
        ):
            raise RuntimeError("Incompatible chat_task_status_state checks")
    if _table_exists(conn, "task_chat_status_effects"):
        if _foreign_keys(conn, "task_chat_status_effects") != {
            ("task_id", "tasks", "id", "NO ACTION", "CASCADE"),
            ("chat_id", "chats", "id", "NO ACTION", "CASCADE"),
            ("chat_status_id", "chat_statuses", "id", "NO ACTION", "SET NULL"),
        }:
            raise RuntimeError("Incompatible task_chat_status_effects foreign keys")
        if _unique_constraint_columns(conn, "task_chat_status_effects") != {("task_id", "cycle")}:
            raise RuntimeError("Incompatible task_chat_status_effects uniqueness")
        if "check(cycle>0)" not in _normalized_table_sql(conn, "task_chat_status_effects"):
            raise RuntimeError("Incompatible task_chat_status_effects cycle check")


def _validate_automation_schema(conn: sqlite3.Connection) -> None:
    _validate_schema_migrations(conn)
    _validate_table_contracts(conn)
    _validate_existing_structural_contracts(conn)
    for table in _AUTOMATION_TABLES:
        foreign_key_errors = conn.execute(f"PRAGMA foreign_key_check({table})").fetchall()
        if foreign_key_errors:
            raise RuntimeError("Incompatible task chat-status foreign-key data")

    for table, name, unique, columns, partial, predicate in _EXPECTED_INDEXES:
        _assert_index_definition(conn, table, name, unique, columns, partial, predicate)
    custom_unique_indexes = {
        str(row["name"])
        for row in conn.execute("PRAGMA index_list(task_chat_status_effects)")
        if bool(row["unique"]) and str(row["origin"]) == "c"
    }
    if custom_unique_indexes != {"idx_task_chat_effects_one_active_task"}:
        raise RuntimeError("Incompatible task_chat_status_effects unique indexes")


def apply_task_chat_status_automation_migration(conn: sqlite3.Connection) -> None:
    """Create and validate the additive task/chat-status automation schema.

    The validator intentionally runs on every startup. A migration marker alone
    must never make a partially copied or incompatible schema writable.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _validate_schema_migrations(conn)
    missing_audit = _validate_table_contracts(conn, allow_missing_audit=True)
    _validate_existing_structural_contracts(conn)
    _validate_existing_index_contracts(conn)
    if missing_audit:
        nonempty = [
            table
            for table in _AUTOMATION_TABLES
            if _table_exists(conn, table)
            and int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"] or 0) > 0
        ]
        if nonempty:
            raise RuntimeError(
                "Incompatible nonempty task chat-status schema: missing trustworthy audit columns"
            )
        for table in _AUTOMATION_TABLES:
            if _table_exists(conn, table):
                conn.execute(f"DROP TABLE {table}")
    _create_automation_tables(conn)

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_task_chat_effects_chat_cycle_active
        ON task_chat_status_effects(chat_id, cycle, released_at, applied_at DESC, id DESC)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_chat_effects_one_active_task
        ON task_chat_status_effects(task_id)
        WHERE released_at IS NULL
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_type_chat_status_links_status ON task_type_chat_status_links(chat_status_id)"
    )
    _validate_automation_schema(conn)
    marker = conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(name) VALUES (?)", (MIGRATION_NAME,)
    )
    if marker.rowcount not in (0, 1):
        raise RuntimeError("Task chat-status migration marker changed unexpectedly")
    _validate_automation_schema(conn)
    if not conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)
    ).fetchone():
        raise RuntimeError("Task chat-status migration marker was not persisted")
    reconcile_automation_state_conn(conn)


def active_mapping_conn(conn: sqlite3.Connection, task_type_id: int | None) -> sqlite3.Row | None:
    if not task_type_id:
        return None
    return conn.execute(
        """
        SELECT l.chat_status_id, s.key AS status_key
        FROM task_type_chat_status_links l
        JOIN task_types tt ON tt.id=l.task_type_id AND tt.is_active=1
        JOIN chat_statuses s ON s.id=l.chat_status_id AND s.is_active=1
        WHERE l.task_type_id=?
        """,
        (int(task_type_id),),
    ).fetchone()


def set_task_type_mapping_conn(
    conn: sqlite3.Connection,
    task_type_id: int,
    chat_status_id: int | None,
) -> None:
    if chat_status_id is None:
        conn.execute("DELETE FROM task_type_chat_status_links WHERE task_type_id=?", (int(task_type_id),))
        return
    status = conn.execute(
        "SELECT id FROM chat_statuses WHERE id=? AND is_active=1",
        (int(chat_status_id),),
    ).fetchone()
    if not status:
        raise ValueError("Выбранный статус чата не существует или неактивен")
    task_type = conn.execute(
        "SELECT id FROM task_types WHERE id=?",
        (int(task_type_id),),
    ).fetchone()
    if not task_type:
        raise ValueError("Тип задачи не существует")
    conn.execute(
        """
        INSERT INTO task_type_chat_status_links(task_type_id, chat_status_id, created_at, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(task_type_id) DO UPDATE SET
            chat_status_id=excluded.chat_status_id,
            updated_at=CURRENT_TIMESTAMP
        """,
        (int(task_type_id), int(chat_status_id)),
    )


def release_task_type_effects_conn(
    conn: sqlite3.Connection,
    task_type_id: int,
    *,
    reason: str = "task_type_deactivated",
) -> int:
    rows = conn.execute(
        """
        SELECT e.id, e.chat_id
        FROM task_chat_status_effects e
        JOIN tasks t ON t.id=e.task_id
        WHERE t.task_type_id=? AND e.released_at IS NULL
        ORDER BY e.id
        """,
        (int(task_type_id),),
    ).fetchall()
    if not rows:
        return 0
    effect_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in effect_ids)
    cursor = conn.execute(
        f"""
        UPDATE task_chat_status_effects
        SET released_at=CURRENT_TIMESTAMP, release_reason=?
        WHERE id IN ({placeholders}) AND released_at IS NULL
        """,
        [str(reason)[:80], *effect_ids],
    )
    if cursor.rowcount != len(effect_ids):
        raise RuntimeError("Task-type effect release changed concurrently")
    for chat_id in sorted({int(row["chat_id"]) for row in rows}):
        recalculate_chat_status_conn(conn, chat_id)
    return len(effect_ids)


def _state_conn(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chat_task_status_state WHERE chat_id=?", (int(chat_id),)).fetchone()


def _chat_status_conn(conn: sqlite3.Connection, chat_id: int) -> str:
    row = conn.execute("SELECT status FROM chats WHERE id=?", (int(chat_id),)).fetchone()
    if not row:
        raise RuntimeError("Chat status write lost its chat row")
    return str(row["status"] or "")


def _set_chat_status_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    status_key: str,
    *,
    expected_status: str | None = None,
) -> None:
    params: list[Any] = [str(status_key), int(chat_id)]
    predicate = "id=?"
    if expected_status is not None:
        predicate += " AND status=?"
        params.append(str(expected_status))
    cursor = conn.execute(
        f"UPDATE chats SET status=?, updated_at=CURRENT_TIMESTAMP WHERE {predicate}",
        params,
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Chat status changed concurrently")


def _set_status_owner_metadata_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    owner: str,
    status_key: str,
) -> None:
    row = conn.execute("SELECT metadata_json FROM chats WHERE id=?", (int(chat_id),)).fetchone()
    if not row:
        raise RuntimeError("Chat metadata write lost its chat row")
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    for key in tuple(metadata):
        if str(key).startswith("_crm_status_manual") or str(key).startswith("_crm_status_provider_override"):
            metadata.pop(key, None)
    if owner == "provider":
        metadata["_crm_status_provider_override"] = True
        metadata["_crm_status_provider_override_value"] = str(status_key)
    cursor = conn.execute(
        "UPDATE chats SET metadata_json=? WHERE id=?",
        (json.dumps(metadata, ensure_ascii=False), int(chat_id)),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Chat status owner metadata changed concurrently")


def _ensure_state_conn(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row:
    state = _state_conn(conn, chat_id)
    if state:
        return state
    baseline = _chat_status_conn(conn, chat_id)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO chat_task_status_state(
            chat_id, cycle, baseline_status_key, override_kind,
            active_task_id, active_status_key, updated_at
        ) VALUES (?, 0, ?, NULL, NULL, NULL, CURRENT_TIMESTAMP)
        """,
        (int(chat_id), baseline),
    )
    if cursor.rowcount not in (0, 1):
        raise RuntimeError("Could not initialize chat task-status state")
    state = _state_conn(conn, chat_id)
    if not state:
        raise RuntimeError("Chat task-status state disappeared")
    return state


def _active_effect_count_conn(conn: sqlite3.Connection, chat_id: int, cycle: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM task_chat_status_effects
        WHERE chat_id=? AND cycle=? AND released_at IS NULL
        """,
        (int(chat_id), int(cycle)),
    ).fetchone()
    return int(row["count"] or 0)


def _release_cycle_effects_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    cycle: int,
    reason: str,
) -> int:
    cursor = conn.execute(
        """
        UPDATE task_chat_status_effects
        SET released_at=COALESCE(released_at, CURRENT_TIMESTAMP),
            release_reason=COALESCE(release_reason, ?)
        WHERE chat_id=? AND cycle=? AND released_at IS NULL
        """,
        (str(reason)[:80], int(chat_id), int(cycle)),
    )
    return max(0, int(cursor.rowcount))


def _safe_baseline_conn(
    conn: sqlite3.Connection,
    baseline_status_key: str | None,
    *,
    excluded_status_keys: set[str] | None = None,
) -> str:
    excluded = {str(item) for item in (excluded_status_keys or set())}
    baseline = str(baseline_status_key or "").strip()
    if baseline and baseline not in excluded:
        row = conn.execute(
            "SELECT key FROM chat_statuses WHERE key=? AND is_active=1", (baseline,)
        ).fetchone()
        if row:
            return str(row["key"])
    clauses = ["is_active=1"]
    params: list[Any] = []
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        clauses.append(f"key NOT IN ({placeholders})")
        params.extend(sorted(excluded))
    fallback = conn.execute(
        f"""
        SELECT key FROM chat_statuses
        WHERE {' AND '.join(clauses)}
        ORDER BY CASE WHEN key='new' THEN 0 ELSE 1 END, sort_order, id
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not fallback:
        raise RuntimeError("No active chat status is available for baseline recovery")
    return str(fallback["key"])


def recalculate_chat_status_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    *,
    excluded_status_keys: set[str] | None = None,
) -> None:
    state = _state_conn(conn, chat_id)
    if not state:
        return
    cycle = int(state["cycle"] or 0)
    if cycle <= 0:
        return

    # A status or task type can be deactivated after acquisition. Such an effect
    # cannot keep applying hidden automation and is released before winner selection.
    conn.execute(
        """
        UPDATE task_chat_status_effects
        SET released_at=CURRENT_TIMESTAMP, release_reason='inactive_mapping'
        WHERE chat_id=? AND cycle=? AND released_at IS NULL
          AND (
              NOT EXISTS (
                  SELECT 1 FROM chat_statuses s
                  WHERE s.id=task_chat_status_effects.chat_status_id AND s.is_active=1
              )
              OR NOT EXISTS (
                  SELECT 1
                  FROM tasks t
                  JOIN task_types tt ON tt.id=t.task_type_id AND tt.is_active=1
                  WHERE t.id=task_chat_status_effects.task_id
              )
          )
        """,
        (int(chat_id), cycle),
    )
    winner = conn.execute(
        """
        SELECT e.task_id, e.mapped_status_key
        FROM task_chat_status_effects e
        JOIN tasks t ON t.id=e.task_id
        JOIN task_types tt ON tt.id=t.task_type_id AND tt.is_active=1
        JOIN chat_statuses s ON s.id=e.chat_status_id AND s.is_active=1
        WHERE e.chat_id=? AND e.cycle=? AND e.released_at IS NULL
          AND lower(t.status) NOT IN ('done', 'archived', 'cancelled')
        ORDER BY e.applied_at DESC, e.task_id DESC, e.id DESC
        LIMIT 1
        """,
        (int(chat_id), cycle),
    ).fetchone()
    if winner:
        current = _chat_status_conn(conn, chat_id)
        status_key = str(winner["mapped_status_key"])
        if current != status_key:
            _set_chat_status_conn(conn, chat_id, status_key, expected_status=current)
        cursor = conn.execute(
            """
            UPDATE chat_task_status_state
            SET active_task_id=?, active_status_key=?, override_kind=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE chat_id=? AND cycle=?
            """,
            (int(winner["task_id"]), status_key, int(chat_id), cycle),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Task status winner state changed concurrently")
        return

    cursor = conn.execute(
        """
        UPDATE chat_task_status_state
        SET active_task_id=NULL, active_status_key=NULL, updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND cycle=?
        """,
        (int(chat_id), cycle),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Task status release state changed concurrently")
    if state["override_kind"] is None:
        baseline = _safe_baseline_conn(
            conn,
            state["baseline_status_key"],
            excluded_status_keys=excluded_status_keys,
        )
        current = _chat_status_conn(conn, chat_id)
        if current != baseline:
            _set_chat_status_conn(conn, chat_id, baseline, expected_status=current)


def release_chat_status_automation_conn(
    conn: sqlite3.Connection,
    chat_status_id: int,
    status_key: str,
) -> tuple[int, int]:
    """Release automation references before a chat status is disabled or deleted."""
    status_id = int(chat_status_id)
    key = str(status_key)
    effect_rows = conn.execute(
        """
        SELECT id, chat_id
        FROM task_chat_status_effects
        WHERE chat_status_id=? AND released_at IS NULL
        ORDER BY id
        """,
        (status_id,),
    ).fetchall()
    current_chat_rows = conn.execute(
        "SELECT id FROM chats WHERE status=? ORDER BY id", (key,)
    ).fetchall()
    affected_chat_ids = {
        *(int(row["chat_id"]) for row in effect_rows),
        *(int(row["id"]) for row in current_chat_rows),
    }
    if affected_chat_ids:
        # Fail before any ownership/configuration write when no safe active target
        # remains. Per-chat baseline selection below uses the same resolver.
        _safe_baseline_conn(conn, None, excluded_status_keys={key})

    if effect_rows:
        effect_ids = [int(row["id"]) for row in effect_rows]
        placeholders = ",".join("?" for _ in effect_ids)
        cursor = conn.execute(
            f"""
            UPDATE task_chat_status_effects
            SET released_at=CURRENT_TIMESTAMP, release_reason='chat_status_removed'
            WHERE id IN ({placeholders}) AND released_at IS NULL
            """,
            effect_ids,
        )
        if cursor.rowcount != len(effect_ids):
            raise RuntimeError("Chat-status effect release changed concurrently")

    for chat_id in sorted(affected_chat_ids):
        recalculate_chat_status_conn(conn, chat_id, excluded_status_keys={key})
        current = _chat_status_conn(conn, chat_id)
        if current == key:
            state = _state_conn(conn, chat_id)
            fallback = _safe_baseline_conn(
                conn,
                state["baseline_status_key"] if state else None,
                excluded_status_keys={key},
            )
            _set_chat_status_conn(conn, chat_id, fallback, expected_status=current)
            if state and state["override_kind"] == "provider":
                _set_status_owner_metadata_conn(conn, chat_id, "provider", fallback)

    mapping_count = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM task_type_chat_status_links WHERE chat_status_id=?",
            (status_id,),
        ).fetchone()["c"]
        or 0
    )
    mapping_cursor = conn.execute(
        "DELETE FROM task_type_chat_status_links WHERE chat_status_id=?", (status_id,)
    )
    if mapping_cursor.rowcount != mapping_count:
        raise RuntimeError("Chat-status mapping release changed concurrently")
    return len(effect_rows), mapping_count


def acquire_task_effect_conn(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    force_new_cycle: bool = False,
) -> bool:
    task = conn.execute(
        """
        SELECT t.id, t.chat_id, t.task_type_id, t.status, c.marketplace
        FROM tasks t JOIN chats c ON c.id=t.chat_id
        WHERE t.id=?
        """,
        (int(task_id),),
    ).fetchone()
    if not task or str(task["status"] or "").lower() in TERMINAL_TASK_STATUSES:
        return False
    if str(task["marketplace"] or "") == "internal_tasks":
        return False
    mapping = active_mapping_conn(conn, task["task_type_id"])
    if not mapping:
        return False

    chat_id = int(task["chat_id"])
    state = _ensure_state_conn(conn, chat_id)
    current_cycle = int(state["cycle"] or 0)
    active_count = _active_effect_count_conn(conn, chat_id, current_cycle) if current_cycle else 0
    starts_new_cycle = bool(
        force_new_cycle
        or current_cycle <= 0
        or state["override_kind"] is not None
        or active_count == 0
    )
    if starts_new_cycle:
        if current_cycle > 0:
            _release_cycle_effects_conn(conn, chat_id, current_cycle, "cycle_replaced")
        current_cycle += 1
        baseline = _chat_status_conn(conn, chat_id)
        cursor = conn.execute(
            """
            UPDATE chat_task_status_state
            SET cycle=?, baseline_status_key=?, override_kind=NULL,
                active_task_id=NULL, active_status_key=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE chat_id=? AND cycle=?
            """,
            (current_cycle, baseline, chat_id, int(state["cycle"] or 0)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Task status cycle changed concurrently")
        _set_status_owner_metadata_conn(conn, chat_id, "task", str(mapping["status_key"]))

    conn.execute(
        """
        INSERT INTO task_chat_status_effects(
            task_id, chat_id, cycle, chat_status_id, mapped_status_key, applied_at
        ) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (
            int(task_id),
            chat_id,
            current_cycle,
            int(mapping["chat_status_id"]),
            str(mapping["status_key"]),
        ),
    )
    recalculate_chat_status_conn(conn, chat_id)
    return True


def release_task_effect_conn(conn: sqlite3.Connection, task_id: int, reason: str) -> bool:
    rows = conn.execute(
        """
        SELECT DISTINCT chat_id
        FROM task_chat_status_effects
        WHERE task_id=? AND released_at IS NULL
        """,
        (int(task_id),),
    ).fetchall()
    cursor = conn.execute(
        """
        UPDATE task_chat_status_effects
        SET released_at=CURRENT_TIMESTAMP, release_reason=?
        WHERE task_id=? AND released_at IS NULL
        """,
        (str(reason)[:80], int(task_id)),
    )
    for row in rows:
        recalculate_chat_status_conn(conn, int(row["chat_id"]))
    return cursor.rowcount > 0


def rebind_task_effect_conn(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    reactivation: bool = False,
) -> bool:
    release_task_effect_conn(conn, task_id, "reactivated" if reactivation else "type_changed")
    return acquire_task_effect_conn(conn, task_id, force_new_cycle=reactivation)


def _record_override_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    override_kind: str,
    status_key: str,
) -> None:
    if override_kind not in {"manual", "provider"}:
        raise ValueError("Unsupported task-status override kind")
    state = _ensure_state_conn(conn, chat_id)
    cycle = int(state["cycle"] or 0)
    if cycle > 0:
        _release_cycle_effects_conn(conn, chat_id, cycle, f"{override_kind}_override")
    current = _chat_status_conn(conn, chat_id)
    if current != status_key:
        _set_chat_status_conn(conn, chat_id, status_key, expected_status=current)
    if override_kind == "provider":
        _set_status_owner_metadata_conn(conn, chat_id, "provider", status_key)
    cursor = conn.execute(
        """
        UPDATE chat_task_status_state
        SET override_kind=?, active_task_id=NULL, active_status_key=NULL,
            updated_at=CURRENT_TIMESTAMP
        WHERE chat_id=? AND cycle=?
        """,
        (override_kind, int(chat_id), cycle),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Task status override state changed concurrently")


def apply_manual_status_conn(conn: sqlite3.Connection, chat_id: int, status_key: str) -> None:
    _record_override_conn(conn, int(chat_id), "manual", str(status_key))


def apply_provider_reopen_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    latest_direction: str | None,
) -> bool:
    current = _chat_status_conn(conn, int(chat_id))
    if current != "closed" or latest_direction != "inbound":
        return False
    target = "new"
    target_row = conn.execute(
        "SELECT key FROM chat_statuses WHERE key=? AND is_active=1", (target,)
    ).fetchone()
    if not target_row:
        target = _safe_baseline_conn(conn, "new")
    _record_override_conn(conn, int(chat_id), "provider", target)
    return True


def reconcile_automation_state_conn(conn: sqlite3.Connection) -> None:
    for state in conn.execute("SELECT chat_id, cycle, override_kind FROM chat_task_status_state ORDER BY chat_id"):
        chat_id = int(state["chat_id"])
        cycle = int(state["cycle"] or 0)
        if state["override_kind"] in {"manual", "provider"}:
            if cycle > 0:
                _release_cycle_effects_conn(conn, chat_id, cycle, "override_reconciled")
            conn.execute(
                """
                UPDATE chat_task_status_state
                SET active_task_id=NULL, active_status_key=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE chat_id=?
                """,
                (chat_id,),
            )
            continue
        recalculate_chat_status_conn(conn, chat_id)


def release_all_task_effects_for_rollback_conn(conn: sqlite3.Connection) -> int:
    released = 0
    states = conn.execute(
        "SELECT chat_id, cycle, override_kind FROM chat_task_status_state ORDER BY chat_id"
    ).fetchall()
    for state in states:
        chat_id = int(state["chat_id"])
        cycle = int(state["cycle"] or 0)
        if cycle <= 0:
            continue
        released += _release_cycle_effects_conn(conn, chat_id, cycle, "rollback_release")
        if state["override_kind"] in {"manual", "provider"}:
            continue
        conn.execute(
            """
            UPDATE chat_task_status_state
            SET override_kind='rollback_release', active_task_id=NULL,
                active_status_key=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE chat_id=? AND cycle=?
            """,
            (chat_id, cycle),
        )
        baseline_row = conn.execute(
            "SELECT baseline_status_key FROM chat_task_status_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        baseline = _safe_baseline_conn(conn, baseline_row["baseline_status_key"])
        current = _chat_status_conn(conn, chat_id)
        if current != baseline:
            _set_chat_status_conn(conn, chat_id, baseline, expected_status=current)
    return released

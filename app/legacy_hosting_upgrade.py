from __future__ import annotations

"""One-time conversion of the exact, code-verified historical hosting schema.

No legacy operation becomes dispatchable. Audit rows retain the original data;
only the deliberately small review representation may leave this DB boundary.
"""
import hashlib
import json
import re
import sqlite3
import unicodedata
from typing import Any, Callable

from app.message_outbox_migration import (
    apply_message_send_operation_migration, assert_message_send_operation_schema,
)
from app.task_chat_status_automation import (
    TASK_CHAT_STATUS_EFFECTS_SQL, apply_task_chat_status_automation_migration,
)

MIGRATION = "20261019_legacy_hosting_review_upgrade"
OLD_OUTBOX_MARKER = "20260806_message_send_outbox"
AUDIT_TABLES = {
    "message_send_operations": "legacy_message_send_audit",
    "chat_task_status_state": "legacy_task_status_audit",
    "task_type_chat_status_links": "legacy_task_mapping_audit",
}
LEGACY_OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS message_send_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_operation_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    marketplace TEXT NOT NULL,
    external_chat_id TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'text' CHECK(message_type IN ('text')),
    payload_hash TEXT NOT NULL,
    author_user_id INTEGER,
    author_label TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'accepted', 'confirmed', 'failed', 'uncertain')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    baseline_message_created_at TEXT,
    provider_response_json TEXT NOT NULL DEFAULT '{}',
    external_message_id TEXT,
    canonical_message_id INTEGER,
    error_code TEXT,
    error_detail TEXT,
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sending_at TEXT,
    accepted_at TEXT,
    confirmed_at TEXT,
    failed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, client_operation_id),
    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY(author_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY(canonical_message_id) REFERENCES messages(id) ON DELETE SET NULL
        );
"""
LEGACY_TASK_SQL = """
CREATE TABLE IF NOT EXISTS task_type_chat_status_links (
    task_type_id INTEGER PRIMARY KEY,
    chat_status_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE CASCADE,
    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS chat_task_status_state (
    chat_id INTEGER PRIMARY KEY,
    baseline_status TEXT NOT NULL,
    baseline_was_manual INTEGER NOT NULL DEFAULT 0,
    active_task_id INTEGER,
    active_status_key TEXT,
    cycle_id TEXT NOT NULL,
    manual_override INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
    FOREIGN KEY(active_task_id) REFERENCES tasks(id) ON DELETE SET NULL
    );
"""
LEGACY_INDEX_SQL = (
"""
CREATE INDEX IF NOT EXISTS idx_message_send_operations_reconcile
    ON message_send_operations(chat_id, message_type, payload_hash, status, requested_at, id);
""",
"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_send_operations_external_unique
    ON message_send_operations(chat_id, external_message_id)
    WHERE external_message_id IS NOT NULL AND TRIM(external_message_id)<>'';
""",
"""
CREATE INDEX IF NOT EXISTS idx_message_send_operations_status_updated
    ON message_send_operations(status, updated_at);
""",
"""
CREATE INDEX IF NOT EXISTS idx_task_status_effect_chat_applied
    ON tasks(chat_id, chat_status_effect_applied_at DESC, id DESC)
    WHERE chat_status_effect_key IS NOT NULL;
""",
"""
CREATE INDEX IF NOT EXISTS idx_task_type_chat_status_links_status
    ON task_type_chat_status_links(chat_status_id, task_type_id);
""",
)
REVIEW_SUMMARY = "Historical send requires manual verification; automatic resend is disabled"
REVIEW_TEXT = "Старая отправка требует ручной сверки; текст не сохранён"
MANIFEST_SQL = """CREATE TABLE legacy_hosting_audit_manifest(
    table_name TEXT PRIMARY KEY, row_count INTEGER NOT NULL, sha256 TEXT NOT NULL,
    operation_highwater INTEGER NOT NULL)"""
LINKS_SQL = """CREATE TABLE legacy_message_review_links(
    operation_id INTEGER PRIMARY KEY,
    canonical_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    FOREIGN KEY(operation_id) REFERENCES legacy_message_send_audit(id) ON DELETE RESTRICT
)"""
AUDIT_INDEX_SQL = (
    "CREATE UNIQUE INDEX ux_legacy_message_audit_id ON legacy_message_send_audit(id)",
    "CREATE UNIQUE INDEX ux_legacy_message_audit_identity ON legacy_message_send_audit(chat_id,client_operation_id)",
    "CREATE INDEX idx_legacy_message_audit_group ON legacy_message_send_audit(chat_id,payload_hash)",
)
AUDIT_FIELDS = {
    "legacy_message_send_audit": (
        "id client_operation_id chat_id marketplace external_chat_id message_type payload_hash "
        "author_user_id author_label status attempt_count baseline_message_created_at "
        "provider_response_json external_message_id canonical_message_id error_code error_detail "
        "requested_at sending_at accepted_at confirmed_at failed_at updated_at"
    ).split(),
    "legacy_task_status_audit": (
        "chat_id baseline_status baseline_was_manual active_task_id active_status_key cycle_id "
        "manual_override created_at updated_at"
    ).split(),
    "legacy_task_mapping_audit": "task_type_id chat_status_id created_at updated_at".split(),
    "legacy_task_effect_audit": (
        "task_id chat_id task_type_id chat_status_effect_key chat_status_effect_applied_at chat_status_effect_cycle"
    ).split(),
}
AUDIT_INTEGER_FIELDS = {
    "id", "chat_id", "author_user_id", "attempt_count", "canonical_message_id",
    "baseline_was_manual", "active_task_id", "manual_override", "task_type_id",
    "chat_status_id", "task_id",
}

def legacy_text_hash(text: Any) -> str:
    """The single boundary for the original hosted text hash, not a new send hash."""
    value = unicodedata.normalize("NFC", str(text or ""))
    value = re.sub(r"\r\n?|\u2028|\u2029", "\n", value).replace("\u00a0", " ")
    value = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", value)
    value = re.sub(r"[\t ]+$", "", value, flags=re.MULTILINE).strip("\n\t ")
    return hashlib.sha256(("text\0" + value).encode("utf-8")).hexdigest()

def _exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

# SQLite stores object names as "name" after ALTER TABLE ... RENAME and after a
# .dump/.recover rebuild: CREATE TABLE "t", REFERENCES "t"(...), ON "t"(...).
_RENAMED_OBJECT_NAME = re.compile(
    r'\b(table|index|trigger|references|on)\s+((?:if\s+not\s+exists\s+)?)"([a-z_][a-z0-9_]*)"'
)

def _sql(value: str) -> str:
    # Normalize only SQL outside string literals; CHECK/default values are data.
    # A plainly quoted object name right after these keywords is the same schema;
    # any other quoted token (double-quoted literals included) stays different.
    parts = re.split(r"('(?:''|[^'])*')", value)
    return "".join(
        part if i % 2 else "".join(
            _RENAMED_OBJECT_NAME.sub(r"\1 \2\3", part.lower()).split()
        ).replace("ifnotexists", "")
        for i, part in enumerate(parts)
    ).rstrip(";")

def _fail() -> None:
    # Deliberately no row values, SQL payloads, provider errors or exception text.
    raise RuntimeError("Unsupported legacy hosting schema; coherent offline upgrade required")

def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return str(row["sql"] or "") if row else ""

def _legacy_tables() -> dict[str, str]:
    task_statements = [part.strip() for part in LEGACY_TASK_SQL.split(";") if part.strip()]
    return {
        "message_send_operations": LEGACY_OUTBOX_SQL,
        "task_type_chat_status_links": task_statements[0],
        "chat_task_status_state": task_statements[1],
    }

def _preflight(conn: sqlite3.Connection) -> None:
    # Exact DDL includes types, defaults, nullability, PK/UNIQUE/CHECK and FK
    # actions. An apparently similar partial schema is NOT a migration candidate.
    for table, expected in _legacy_tables().items():
        if _sql(_table_sql(conn, table)) != _sql(expected):
            _fail()
    if any(_exists(conn, name) for name in AUDIT_TABLES.values()):
        _fail()
    if _exists(conn, "task_chat_status_effects"):
        _require_unused_effects_table(conn)
    if _sql(_table_sql(conn, "schema_migrations")) != _sql(
        "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ):
        _fail()
    markers = {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}
    if OLD_OUTBOX_MARKER not in markers or markers.intersection({
        "20260814_message_send_command_outbox", "20260824_task_type_chat_status_automation", MIGRATION
    }):
        _fail()
    for statement in LEGACY_INDEX_SQL:
        name = statement.split("EXISTS ", 1)[1].split()[0]
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if not row or _sql(row[0]) != _sql(statement):
            _fail()
    expected_indexes = {
        "message_send_operations": {
            "idx_message_send_operations_reconcile", "idx_message_send_operations_external_unique",
            "idx_message_send_operations_status_updated",
        },
        "task_type_chat_status_links": {"idx_task_type_chat_status_links_status"},
        "chat_task_status_state": set(),
    }
    for table, names in expected_indexes.items():
        actual = {str(r["name"]) for r in conn.execute(f"PRAGMA index_list({table})") if r["origin"] == "c"}
        if actual != names:
            _fail()
    # Parent references and all fields consumed before the normal init schema run.
    required = {
        "chats": {"id", "marketplace", "external_chat_id", "status", "metadata_json", "last_message_preview", "last_message_at"},
        "messages": {"id", "chat_id", "direction", "text", "external_message_id", "raw_json", "author", "created_at",
                     "is_crm_sent", "crm_author_user_id", "crm_author_label", "client_operation_id"},
        "users": {"id", "display_name", "username"},
        "tasks": {"id", "chat_id", "task_type_id", "status", "chat_status_effect_key",
                  "chat_status_effect_applied_at", "chat_status_effect_cycle"},
        "task_types": {"id", "is_active"},
        "chat_statuses": {"id", "key", "is_active", "sort_order"},
    }
    for table, columns in required.items():
        info = {str(r["name"]): r for r in conn.execute(f"PRAGMA table_info({table})")}
        if (not columns.issubset(info) or str(info["id"]["type"]).upper() != "INTEGER"
                or [key for key, row in info.items() if row["pk"]] != ["id"]):
            _fail()
    for name in ("chat_status_effect_key", "chat_status_effect_applied_at", "chat_status_effect_cycle"):
        row = next(r for r in conn.execute("PRAGMA table_info(tasks)") if r["name"] == name)
        if str(row["type"]).upper() != "TEXT" or row["notnull"] or row["dflt_value"] is not None or row["pk"]:
            _fail()
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = str(row[0])
        escaped = name.replace('"', '""')
        referenced = (*AUDIT_TABLES, "task_chat_status_effects")
        if any(str(fk["table"]).lower() in referenced for fk in conn.execute(f'PRAGMA foreign_key_list("{escaped}")')):
            _fail()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('message_send_operations','chat_task_status_state','task_type_chat_status_links')").fetchone():
        _fail()
    # Orphaned immutable task mappings may be quarantined. Operation/chat scope
    # must be reliable; never silently assign an unresolved send to another chat.
    if conn.execute("""
        SELECT 1 FROM message_send_operations o LEFT JOIN chats c ON c.id=o.chat_id
        WHERE c.id IS NULL OR o.marketplace<>c.marketplace OR o.external_chat_id<>c.external_chat_id
           OR o.id<=0 OR trim(o.client_operation_id)='' OR length(o.payload_hash)<>64
           OR o.payload_hash GLOB '*[^0-9a-f]*'
        LIMIT 1
    """).fetchone():
        _fail()

def _require_unused_effects_table(conn: sqlite3.Connection) -> None:
    # A newer release that stopped in its validators leaves this table behind:
    # SQLite commits CREATE TABLE at once. Only a never-used copy of the exact
    # code DDL is rebuilt by the bridge; any trace of use is a mixed schema.
    if _sql(_table_sql(conn, "task_chat_status_effects")) != _sql(TASK_CHAT_STATUS_EFFECTS_SQL):
        _fail()
    if conn.execute("SELECT 1 FROM task_chat_status_effects LIMIT 1").fetchone():
        _fail()
    if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name='task_chat_status_effects'").fetchone():
        _fail()
    indexes = {str(r["name"]) for r in conn.execute("PRAGMA index_list(task_chat_status_effects)") if r["origin"] == "c"}
    if not indexes <= {"idx_task_chat_effects_chat_cycle_active", "idx_task_chat_effects_one_active_task"}:
        _fail()
    # tbl_name of a view is the view itself; dependents are found by their SQL text.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') AND sql LIKE '%task_chat_status_effects%'"
    ).fetchone():
        _fail()

def _trigger_sql(table: str, action: str) -> str:
    return f"""CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table}
        BEGIN SELECT RAISE(ABORT, 'Legacy hosting audit is immutable'); END"""


def _immutable(conn: sqlite3.Connection, table: str) -> None:
    for action in ("UPDATE", "DELETE", "INSERT"):
        conn.execute(_trigger_sql(table, action))

def _digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    digest = hashlib.sha256(b"[")
    count = 0
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
        if count:
            digest.update(b",")
        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        count += 1
    digest.update(b"]")
    return count, digest.hexdigest()

def _archive(conn: sqlite3.Connection) -> None:
    for source, target in AUDIT_TABLES.items():
        # The audit deliberately has no foreign keys: deleting a chat/user/task
        # must not cascade away evidence or rewrite its original IDs.
        conn.execute(f"CREATE TABLE {target} AS SELECT * FROM {source}")
    conn.execute("""CREATE TABLE legacy_task_effect_audit AS
        SELECT id AS task_id, chat_id, task_type_id, chat_status_effect_key,
               chat_status_effect_applied_at, chat_status_effect_cycle FROM tasks""")
    conn.execute(MANIFEST_SQL)
    high = conn.execute("SELECT COALESCE(MAX(id),0) FROM message_send_operations").fetchone()[0]
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='message_send_operations'").fetchone()
    high = max(int(high), int(seq[0]) if seq else 0)
    for table in (*AUDIT_TABLES.values(), "legacy_task_effect_audit"):
        count, digest = _digest(conn, table)
        conn.execute("INSERT INTO legacy_hosting_audit_manifest VALUES(?,?,?,?)", (table, count, digest, high))
        _immutable(conn, table)
    _immutable(conn, "legacy_hosting_audit_manifest")
    # CTAS has no original PK; add uniqueness before establishing review links.
    for statement in AUDIT_INDEX_SQL:
        conn.execute(statement)
    conn.execute(LINKS_SQL)
    for row in conn.execute("SELECT id,canonical_message_id FROM legacy_message_send_audit"):
        linked = row["canonical_message_id"]
        if linked is not None and not conn.execute("SELECT 1 FROM messages WHERE id=?", (linked,)).fetchone():
            linked = None
        conn.execute("INSERT INTO legacy_message_review_links VALUES(?,?)", (row["id"], linked))

def remap_legacy_message_reference(conn: sqlite3.Connection, old_id: int, new_id: int) -> None:
    if _exists(conn, "legacy_message_review_links"):
        conn.execute("UPDATE legacy_message_review_links SET canonical_message_id=? WHERE canonical_message_id=?", (new_id, old_id))

def _validate_ledger(conn: sqlite3.Connection) -> None:
    expected = {*AUDIT_TABLES.values(), "legacy_task_effect_audit"}
    for table, ddl in (("legacy_hosting_audit_manifest", MANIFEST_SQL), ("legacy_message_review_links", LINKS_SQL)):
        if _sql(_table_sql(conn, table)) != _sql(ddl):
            _fail()
    for statement in AUDIT_INDEX_SQL:
        name = statement.split("INDEX ", 1)[1].split()[0]
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
        if not row or _sql(row[0]) != _sql(statement):
            _fail()
    for table in (*expected, "legacy_hosting_audit_manifest"):
        if table in AUDIT_FIELDS:
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if [r["name"] for r in info] != AUDIT_FIELDS[table]:
                _fail()
            for column in info:
                expected_type = "INT" if column["name"] in AUDIT_INTEGER_FIELDS else "TEXT"
                if (column["type"] != expected_type or column["pk"] or column["notnull"]
                        or column["dflt_value"] is not None):
                    _fail()
        actual_triggers = {r["name"]: r["sql"] for r in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )}
        if set(actual_triggers) != {f"{table}_no_{a}" for a in ("update", "delete", "insert")}:
            _fail()
        for action in ("UPDATE", "DELETE", "INSERT"):
            if _sql(actual_triggers[f"{table}_no_{action.lower()}"]) != _sql(_trigger_sql(table, action)):
                _fail()
    rows = conn.execute("SELECT * FROM legacy_hosting_audit_manifest").fetchall()
    if {str(row["table_name"]) for row in rows} != expected:
        _fail()
    for row in rows:
        table = str(row["table_name"])
        if not _exists(conn, table) or conn.execute(f"PRAGMA foreign_key_list({table})").fetchone():
            _fail()
        if _digest(conn, table) != (row["row_count"], row["sha256"]):
            _fail()
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='message_send_operations'").fetchone()
    high = max(int(row["operation_highwater"]) for row in rows)
    if not seq or int(seq[0]) < high:
        _fail()
    if conn.execute("SELECT 1 FROM message_send_operations WHERE id<=? LIMIT 1", (high,)).fetchone():
        _fail()
    if conn.execute("PRAGMA foreign_key_check(legacy_message_review_links)").fetchone():
        _fail()
    if conn.execute("""SELECT 1 FROM legacy_message_send_audit a
        LEFT JOIN legacy_message_review_links l ON l.operation_id=a.id
        WHERE l.operation_id IS NULL LIMIT 1""").fetchone():
        _fail()

def apply_legacy_hosting_upgrade(
    conn: sqlite3.Connection, identity_migration: Callable[[sqlite3.Connection], None],
) -> bool:
    """Run before init_db's first executescript. Own the entire conversion lock."""
    if conn.in_transaction:
        raise RuntimeError("Legacy hosting upgrade requires an idle connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        marker = _exists(conn, "schema_migrations") and conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()
        if marker:
            _validate_ledger(conn)
            assert_message_send_operation_schema(conn)
            apply_task_chat_status_automation_migration(conn)
            conn.commit()
            return False
        outbox_columns = {str(r["name"]).lower() for r in conn.execute("PRAGMA table_info(message_send_operations)")}
        state_columns = {str(r["name"]).lower() for r in conn.execute("PRAGMA table_info(chat_task_status_state)")}
        old_marker = _exists(conn, "schema_migrations") and conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?", (OLD_OUTBOX_MARKER,)
        ).fetchone()
        modern_marker = _exists(conn, "schema_migrations") and conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name='20260814_message_send_command_outbox'"
        ).fetchone()
        legacy = ("message_type" in outbox_columns or "cycle_id" in state_columns
                  or bool(old_marker and not modern_marker))
        if not legacy:
            if any(_exists(conn, name) for name in (*AUDIT_FIELDS, "legacy_hosting_audit_manifest", "legacy_message_review_links")):
                _fail()
            conn.rollback()
            return False
        _preflight(conn)
        if _exists(conn, "task_chat_status_effects"):
            # Verified empty leftover; the automation migration recreates it below.
            conn.execute("DROP TABLE task_chat_status_effects")
        _archive(conn)
        for table in AUDIT_TABLES:
            conn.execute(f"DROP TABLE {table}")
        # Existing identity migration is preserved, with the narrow reference
        # remap hook invoked before every duplicate deletion.
        identity_migration(conn)
        apply_message_send_operation_migration(conn)
        high = conn.execute("SELECT MAX(operation_highwater) FROM legacy_hosting_audit_manifest").fetchone()[0]
        conn.execute("DELETE FROM sqlite_sequence WHERE name='message_send_operations'")
        conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('message_send_operations',?)", (high,))
        apply_task_chat_status_automation_migration(conn)
        conn.execute("""
            INSERT INTO task_type_chat_status_links(task_type_id,chat_status_id,created_at,updated_at)
            SELECT a.task_type_id,a.chat_status_id,a.created_at,a.updated_at
            FROM legacy_task_mapping_audit a
            JOIN task_types t ON t.id=a.task_type_id
            JOIN chat_statuses s ON s.id=a.chat_status_id AND s.is_active=1
        """)
        _validate_ledger(conn)
        conn.execute("INSERT INTO schema_migrations(name) VALUES(?)", (MIGRATION,))
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise

def _review_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    canonical = None
    candidate = conn.execute("""
        SELECT m.* FROM legacy_message_review_links l JOIN messages m ON m.id=l.canonical_message_id
        WHERE l.operation_id=?
    """, (row["id"],)).fetchone()
    if candidate and row["status"] == "confirmed" and (
        candidate["chat_id"] == row["chat_id"] and candidate["direction"] == "outbound"
        and row["external_message_id"] and candidate["external_message_id"] == row["external_message_id"]
        and legacy_text_hash(candidate["text"]) == row["payload_hash"]
    ):
        # A single exact existing reference, not text/time inference or an ACK.
        canonical = int(candidate["id"])
    user = conn.execute("SELECT display_name,username FROM users WHERE id=?", (row["author_user_id"],)).fetchone()
    author = str((user["display_name"] or user["username"] or "") if user else "")
    return {
        "id": int(row["id"]), "chat_id": int(row["chat_id"]),
        "client_operation_id": str(row["client_operation_id"]), "command_kind": "chat_text",
        "intent_origin": "message", "payload": {}, "legacy_review": True,
        "status": "confirmed" if canonical else "uncertain",
        "attempt_count": int(row["attempt_count"]), "author_user_id": row["author_user_id"],
        "author_label": author, "requested_at": row["requested_at"], "next_attempt_at": None,
        "canonical_message_id": canonical,
        "provider_external_message_id": row["external_message_id"] if canonical else None,
        "error_category": None if canonical else "legacy_review",
        "error_summary": None if canonical else REVIEW_SUMMARY,
    }

def get_legacy_review_conn(conn: sqlite3.Connection, *, operation_id: int | None = None,
                           chat_id: int | None = None, client_operation_id: str | None = None) -> dict[str, Any] | None:
    if not _exists(conn, "legacy_message_send_audit"):
        return None
    if operation_id is not None:
        row = conn.execute("SELECT * FROM legacy_message_send_audit WHERE id=?", (operation_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM legacy_message_send_audit WHERE chat_id=? AND client_operation_id=?", (chat_id,client_operation_id)).fetchone()
    return _review_dict(conn, row) if row else None

def list_legacy_reviews_conn(conn: sqlite3.Connection, chat_id: int) -> list[dict[str, Any]]:
    if not _exists(conn, "legacy_message_send_audit"):
        return []
    result = [_review_dict(conn, row) for row in conn.execute("SELECT * FROM legacy_message_send_audit WHERE chat_id=? ORDER BY id DESC", (chat_id,))]
    return [row for row in result if row["status"] != "confirmed"]

def guard_legacy_registration_conn(conn: sqlite3.Connection, *, chat_id: int,
                                   client_operation_id: str, original_text: str) -> dict[str, Any] | None:
    if not _exists(conn, "legacy_message_send_audit"):
        return None
    digest = legacy_text_hash(original_text)
    existing = conn.execute("SELECT * FROM legacy_message_send_audit WHERE chat_id=? AND client_operation_id=?", (chat_id,client_operation_id)).fetchone()
    if existing:
        if existing["payload_hash"] != digest:
            raise ValueError("Historical operation id has a different payload")
        return _review_dict(conn, existing)
    for row in conn.execute("SELECT * FROM legacy_message_send_audit WHERE chat_id=? AND payload_hash=?", (chat_id,digest)):
        if _review_dict(conn, row)["status"] != "confirmed":
            raise ValueError("Historical send outcome requires manual verification")
    return None

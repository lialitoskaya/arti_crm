from __future__ import annotations

import sqlite3
from typing import Final

from app.message_send_models import (
    MESSAGE_SEND_ACTIVE_RECONCILIATION_STATUSES,
    MESSAGE_SEND_RECONCILIATION_STATUS_SQL,
    MESSAGE_SEND_STATUSES,
)


MESSAGE_SEND_OPERATION_MIGRATION: Final = "20260814_message_send_command_outbox"
MESSAGE_SEND_OPERATION_TABLE: Final = "message_send_operations"

MESSAGE_SEND_REQUIRED_COLUMNS: Final[dict[str, str]] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "chat_id": "INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE",
    "marketplace": "TEXT NOT NULL DEFAULT ''",
    "external_chat_id": "TEXT NOT NULL DEFAULT ''",
    "client_operation_id": "TEXT NOT NULL DEFAULT ''",
    "command_kind": "TEXT NOT NULL DEFAULT 'chat_text'",
    "intent_origin": "TEXT NOT NULL DEFAULT 'message'",
    "payload_json": "TEXT NOT NULL DEFAULT '{}'",
    "payload_hash": "TEXT NOT NULL DEFAULT ''",
    "author_user_id": "INTEGER REFERENCES users(id) ON DELETE SET NULL",
    "author_label": "TEXT NOT NULL DEFAULT ''",
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "next_attempt_at": "TEXT",
    "claim_token": "TEXT",
    "lease_until": "TEXT",
    "requested_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "claimed_at": "TEXT",
    "last_attempt_at": "TEXT",
    "accepted_at": "TEXT",
    "confirmed_at": "TEXT",
    "failed_at": "TEXT",
    "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "provider_external_message_id": "TEXT",
    "canonical_message_id": "INTEGER REFERENCES messages(id) ON DELETE SET NULL",
    "error_category": "TEXT",
    "error_http_status": "INTEGER",
    "error_provider_code": "TEXT",
    "error_correlation_id": "TEXT",
    "error_summary": "TEXT",
}


def _create_table(conn: sqlite3.Connection) -> None:
    status_sql = ", ".join(f"'{status}'" for status in MESSAGE_SEND_STATUSES)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSAGE_SEND_OPERATION_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            marketplace TEXT NOT NULL,
            external_chat_id TEXT NOT NULL,
            client_operation_id TEXT NOT NULL,
            command_kind TEXT NOT NULL DEFAULT 'chat_text'
                CHECK(command_kind='chat_text'),
            intent_origin TEXT NOT NULL DEFAULT 'message'
                CHECK(intent_origin IN ('message', 'attachment_caption')),
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            author_label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ({status_sql})),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            next_attempt_at TEXT,
            claim_token TEXT,
            lease_until TEXT,
            requested_at TEXT NOT NULL,
            claimed_at TEXT,
            last_attempt_at TEXT,
            accepted_at TEXT,
            confirmed_at TEXT,
            failed_at TEXT,
            updated_at TEXT NOT NULL,
            provider_external_message_id TEXT,
            canonical_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            error_category TEXT,
            error_http_status INTEGER,
            error_provider_code TEXT,
            error_correlation_id TEXT,
            error_summary TEXT
        )
        """
    )


def _normalized_sql(value: object) -> str:
    return "".join(str(value or "").lower().split())


def _table_contract_errors(conn: sqlite3.Connection) -> list[str]:
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (MESSAGE_SEND_OPERATION_TABLE,),
    ).fetchone()
    if not table_row:
        return ["table is missing"]

    column_rows = conn.execute(
        f"PRAGMA table_info({MESSAGE_SEND_OPERATION_TABLE})"
    ).fetchall()
    columns = {str(row["name"]): row for row in column_rows}
    errors: list[str] = []
    missing = set(MESSAGE_SEND_REQUIRED_COLUMNS) - set(columns)
    extra = set(columns) - set(MESSAGE_SEND_REQUIRED_COLUMNS)
    if missing:
        errors.append("missing columns: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("unexpected columns: " + ", ".join(sorted(extra)))
    if "id" in columns and int(columns["id"]["pk"] or 0) != 1:
        errors.append("id is not the primary key")
    required_not_null = {
        "chat_id", "marketplace", "external_chat_id", "client_operation_id",
        "command_kind", "intent_origin", "payload_json", "payload_hash",
        "author_label", "status", "attempt_count", "requested_at", "updated_at",
    }
    nullable = sorted(
        name for name in required_not_null
        if name in columns and int(columns[name]["notnull"] or 0) != 1
    )
    if nullable:
        errors.append("columns are unexpectedly nullable: " + ", ".join(nullable))

    table_sql = _normalized_sql(table_row["sql"])
    status_sql = ",".join(f"'{status}'" for status in MESSAGE_SEND_STATUSES)
    required_checks = (
        "check(command_kind='chat_text')",
        "check(intent_originin('message','attachment_caption'))",
        f"check(statusin({status_sql}))",
        "check(attempt_count>=0)",
    )
    for check_sql in required_checks:
        if check_sql not in table_sql:
            errors.append(f"missing constraint: {check_sql}")

    foreign_keys = {
        (str(row["from"]), str(row["table"]), str(row["to"]), str(row["on_delete"]).upper())
        for row in conn.execute(
            f"PRAGMA foreign_key_list({MESSAGE_SEND_OPERATION_TABLE})"
        ).fetchall()
    }
    expected_foreign_keys = {
        ("chat_id", "chats", "id", "CASCADE"),
        ("author_user_id", "users", "id", "SET NULL"),
        ("canonical_message_id", "messages", "id", "SET NULL"),
    }
    if foreign_keys != expected_foreign_keys:
        errors.append("foreign-key contract differs from the canonical schema")
    return errors


def _complete_partial_table(conn: sqlite3.Connection) -> None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (MESSAGE_SEND_OPERATION_TABLE,),
    ).fetchone():
        return
    errors = _table_contract_errors(conn)
    if not errors:
        return
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {MESSAGE_SEND_OPERATION_TABLE}"
    ).fetchone()
    if int(row["count"] or 0) > 0:
        raise RuntimeError(
            "Nonempty partial message send operation table cannot be reconstructed safely: "
            + "; ".join(errors)
        )
    conn.execute(f"DROP TABLE {MESSAGE_SEND_OPERATION_TABLE}")
    _create_table(conn)


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_message_send_operation_identity
            ON {MESSAGE_SEND_OPERATION_TABLE}(chat_id, client_operation_id);

        CREATE UNIQUE INDEX IF NOT EXISTS ux_message_send_operation_external
            ON {MESSAGE_SEND_OPERATION_TABLE}(chat_id, provider_external_message_id)
            WHERE provider_external_message_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS ux_message_send_operation_message
            ON {MESSAGE_SEND_OPERATION_TABLE}(canonical_message_id)
            WHERE canonical_message_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS ux_message_send_operation_claim_token
            ON {MESSAGE_SEND_OPERATION_TABLE}(claim_token)
            WHERE claim_token IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS ux_message_send_active_reconciliation_group
            ON {MESSAGE_SEND_OPERATION_TABLE}(chat_id, payload_hash)
            WHERE canonical_message_id IS NULL
              AND status IN ({MESSAGE_SEND_RECONCILIATION_STATUS_SQL});

        CREATE INDEX IF NOT EXISTS idx_message_send_operation_drain
            ON {MESSAGE_SEND_OPERATION_TABLE}(status, next_attempt_at, requested_at, id);

        CREATE INDEX IF NOT EXISTS idx_message_send_operation_lease
            ON {MESSAGE_SEND_OPERATION_TABLE}(status, lease_until, id);

        CREATE INDEX IF NOT EXISTS idx_message_send_operation_reconcile
            ON {MESSAGE_SEND_OPERATION_TABLE}(
                chat_id, payload_hash, status, last_attempt_at, id
            );
        """
    )


def assert_message_send_operation_schema(conn: sqlite3.Connection) -> None:
    errors = _table_contract_errors(conn)
    if errors:
        raise RuntimeError("Message send operation schema is incomplete: " + "; ".join(errors))

    expected_indexes = {
        "ux_message_send_operation_identity": True,
        "ux_message_send_operation_external": True,
        "ux_message_send_operation_message": True,
        "ux_message_send_operation_claim_token": True,
        "ux_message_send_active_reconciliation_group": True,
        "idx_message_send_operation_drain": False,
        "idx_message_send_operation_lease": False,
        "idx_message_send_operation_reconcile": False,
    }
    index_rows = {
        str(row["name"]): bool(row["unique"])
        for row in conn.execute(
            f"PRAGMA index_list({MESSAGE_SEND_OPERATION_TABLE})"
        ).fetchall()
    }
    for name, should_be_unique in expected_indexes.items():
        if name not in index_rows:
            raise RuntimeError(f"Message send operation index is missing: {name}")
        if index_rows[name] != should_be_unique:
            raise RuntimeError(f"Message send operation index uniqueness drift: {name}")

    active_status_sql = ",".join(
        f"'{status}'" for status in MESSAGE_SEND_ACTIVE_RECONCILIATION_STATUSES
    )
    expected_index_fragments = {
        "ux_message_send_operation_identity": "(chat_id,client_operation_id)",
        "ux_message_send_operation_external": (
            "(chat_id,provider_external_message_id)"
            "whereprovider_external_message_idisnotnull"
        ),
        "ux_message_send_operation_message": (
            "(canonical_message_id)wherecanonical_message_idisnotnull"
        ),
        "ux_message_send_operation_claim_token": (
            "(claim_token)whereclaim_tokenisnotnull"
        ),
        "ux_message_send_active_reconciliation_group": (
            "(chat_id,payload_hash)wherecanonical_message_idisnull"
            f"andstatusin({active_status_sql})"
        ),
        "idx_message_send_operation_drain": (
            "(status,next_attempt_at,requested_at,id)"
        ),
        "idx_message_send_operation_lease": "(status,lease_until,id)",
        "idx_message_send_operation_reconcile": (
            "(chat_id,payload_hash,status,last_attempt_at,id)"
        ),
    }
    for name, expected_fragment in expected_index_fragments.items():
        index_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        if expected_fragment not in _normalized_sql(index_row["sql"] if index_row else ""):
            raise RuntimeError(f"Message send operation index contract drift: {name}")

    index_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        ("ux_message_send_active_reconciliation_group",),
    ).fetchone()
    index_sql = str(index_row["sql"] if index_row else "")
    for status in MESSAGE_SEND_ACTIVE_RECONCILIATION_STATUSES:
        if f"'{status}'" not in index_sql:
            raise RuntimeError(
                "Active reconciliation status drift between schema and state machine"
            )


def apply_message_send_operation_migration(conn: sqlite3.Connection) -> None:
    """Add the durable text-command operation schema after slice-09 identity."""
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
        (MESSAGE_SEND_OPERATION_MIGRATION,),
    ).fetchone()
    if already_applied:
        assert_message_send_operation_schema(conn)
        return

    _create_table(conn)
    _complete_partial_table(conn)
    _create_indexes(conn)
    assert_message_send_operation_schema(conn)
    conn.execute(
        "INSERT INTO schema_migrations(name) VALUES (?)",
        (MESSAGE_SEND_OPERATION_MIGRATION,),
    )

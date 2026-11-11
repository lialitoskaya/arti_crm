from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest import mock
from wsgiref.util import setup_testing_defaults

import httpx
import pytest
import test_regression_foundation as foundation
from app import db
from app import legacy_hosting_upgrade as upgrade
from app import task_chat_status_automation as automation
from app import repository as repo
from app.schemas import TaskCreate, TaskUpdate, TaskTypeUpdate
from app.message_send_operations import (
    MessageSendOperationConflict, get_operation, get_operation_by_client_id,
    list_chat_operations, register_operation, claim_next_operation,
)
from app.message_send_service import MessageSendService, public_operation
from starlette.datastructures import State

# Static DDL only, transcribed from the supplied hosting code. Never import or
# execute a hosting startup module, credential loader or database.
LEGACY_DDL = """
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
CREATE INDEX IF NOT EXISTS idx_message_send_operations_reconcile
    ON message_send_operations(chat_id, message_type, payload_hash, status, requested_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_message_send_operations_external_unique
    ON message_send_operations(chat_id, external_message_id)
    WHERE external_message_id IS NOT NULL AND TRIM(external_message_id)<>'';
CREATE INDEX IF NOT EXISTS idx_message_send_operations_status_updated
    ON message_send_operations(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_status_effect_chat_applied
    ON tasks(chat_id, chat_status_effect_applied_at DESC, id DESC)
    WHERE chat_status_effect_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_task_type_chat_status_links_status
    ON task_type_chat_status_links(chat_status_id, task_type_id);
"""

def legacy_hash(text):
    import re
    import unicodedata
    text = unicodedata.normalize("NFC", str(text or ""))
    text = re.sub(r"\r\n?|\u2028|\u2029", "\n", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[\u200B-\u200D\u2060\uFEFF]", "", text)
    text = re.sub(r"[\t ]+$", "", text, flags=re.MULTILINE).strip("\n\t ")
    return hashlib.sha256(("text\0" + text).encode()).hexdigest()

class LegacyHostingUpgradeTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def isolated_database(self, tmp_path):
        test_db = (tmp_path / "synthetic-upgrade.sqlite3").resolve()

        def connect(path, *args, **kwargs):
            assert Path(path).resolve() == test_db
            return foundation._REAL_SQLITE_CONNECT(path, *args, **kwargs)

        with mock.patch.object(db, "DATABASE_PATH", str(test_db)), mock.patch.object(
            sqlite3, "connect", side_effect=connect
        ):
            yield

    def setUp(self):
        foundation._NETWORK_ATTEMPTS.clear()
        with db.get_connection() as conn:
            conn.executescript((Path(__file__).parent / "fixtures/legacy_hosting_schema.sql").read_text(encoding="utf-8"))
            conn.executescript(LEGACY_DDL)
            conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            conn.execute("INSERT INTO schema_migrations(name) VALUES ('20260806_message_send_outbox')")
            conn.executemany("INSERT INTO chat_statuses(key,title,is_active) VALUES(?,?,1)", (
                ("new", "New"), ("waiting_customer", "Waiting"), ("closed", "Closed"),
            ))
            conn.execute("INSERT INTO chats(id,marketplace,external_chat_id,status,metadata_json) VALUES(9001,'ozon','legacy-chat','closed','{}')")
            conn.execute("INSERT INTO chats(id,marketplace,external_chat_id,status,metadata_json) VALUES(9002,'ozon','other-chat','new','{}')")

    def tearDown(self):
        self.assertEqual([], foundation._NETWORK_ATTEMPTS)

    def old_operation(self, *, operation_id=41, status="uncertain", text="old text",
                      external_id=None, canonical_id=None, chat_id=9001):
        with db.get_connection() as conn:
            conn.execute("""
                INSERT INTO message_send_operations(
                    id,client_operation_id,chat_id,marketplace,external_chat_id,
                    payload_hash,status,author_label,provider_response_json,error_detail,
                    external_message_id,canonical_message_id
                ) VALUES(?, ?, ?, 'ozon', 'legacy-chat', ?, ?, 'SPOOFED AUTHOR',
                         '{"private":"must not leak"}', 'private error must not leak', ?, ?)
                """, (operation_id, f"old-{operation_id}", chat_id, legacy_hash(text),
                      status, external_id, canonical_id))

    def snapshot(self):
        with db.get_connection() as conn:
            return tuple(conn.iterdump())

    def test_legacy_unknown_outcome_is_preserved_and_not_dispatchable(self):
        self.old_operation()
        db.init_db()
        op = get_operation(41)
        self.assertEqual("uncertain", op["status"])
        self.assertEqual({}, op["payload"])
        self.assertIsNone(claim_next_operation(operation_id=41))
        self.assertEqual(41, get_operation_by_client_id(9001, "old-41")["id"])
        self.assertEqual([41], [row["id"] for row in list_chat_operations(9001)])
        public = public_operation(op)
        self.assertNotIn("private", json.dumps(public))
        self.assertNotIn("SPOOFED AUTHOR", json.dumps(public))
        self.assertEqual("Старая отправка требует ручной сверки; текст не сохранён", public["text"])
        with db.get_connection() as conn:
            old = conn.execute("SELECT * FROM legacy_message_send_audit").fetchone()
            self.assertEqual("uncertain", old["status"])
            self.assertEqual('{"private":"must not leak"}', old["provider_response_json"])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM message_send_operations").fetchone()[0])

    def test_known_empty_and_repeat_init(self):
        db.init_db()
        with db.get_connection() as conn:
            before = {table: list(map(tuple, conn.execute(f"SELECT * FROM {table}"))) for table in (
                "legacy_hosting_audit_manifest", "legacy_message_send_audit", "legacy_task_status_audit",
                "legacy_task_effect_audit", "legacy_task_mapping_audit", "legacy_message_review_links",
            )}
        db.init_db()
        with db.get_connection() as conn:
            for table, rows in before.items():
                self.assertEqual(rows, list(map(tuple, conn.execute(f"SELECT * FROM {table}"))))
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM legacy_message_send_audit").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM task_chat_status_effects").fetchone()[0])

    def test_repeat_operation_and_same_hash_new_id_are_guarded(self):
        self.old_operation(text="e\u0301\r\nhello\u200b  ")
        db.init_db()
        arguments = dict(chat_id=9001, text="é\nhello", intent_origin="message",
                         author_user_id=None, author_label="Employee")
        op, dedup = register_operation(client_operation_id="old-41", **arguments)
        self.assertTrue(dedup)
        self.assertEqual(41, op["id"])
        with self.assertRaises(MessageSendOperationConflict):
            register_operation(client_operation_id="new-id", **arguments)
        arguments["text"] = "different"
        with self.assertRaises(MessageSendOperationConflict):
            register_operation(client_operation_id="old-41", **arguments)
        op, dedup = register_operation(client_operation_id="new-id", **arguments)
        self.assertFalse(dedup)
        self.assertGreater(op["id"], 41)

    def test_unknown_partial_schema_rolls_back_without_writes(self):
        with db.get_connection() as conn:
            conn.execute("ALTER TABLE message_send_operations ADD COLUMN surprise TEXT")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
            db.init_db()
        self.assertEqual(before, self.snapshot())

    def message(self, *, message_id=61, chat_id=9001, direction="outbound",
                text="old text", external_id="provider-1", crm=0):
        with db.get_connection() as conn:
            conn.execute("""INSERT INTO messages(id,chat_id,direction,text,external_message_id,is_crm_sent)
                VALUES(?,?,?,?,?,?)""", (message_id, chat_id, direction, text, external_id, crm))
        return message_id

    def test_accepted_ack_is_not_provider_history_confirmation(self):
        mid = self.message()
        self.old_operation(status="accepted", external_id="provider-1", canonical_id=mid)
        db.init_db()
        self.assertEqual("uncertain", get_operation(41)["status"])
        self.assertEqual([41], [op["id"] for op in list_chat_operations(9001)])

    def test_confirmed_exact_reference_has_one_logical_row(self):
        mid = self.message()
        self.old_operation(status="confirmed", external_id="provider-1", canonical_id=mid)
        db.init_db()
        self.assertEqual("confirmed", get_operation(41)["status"])
        self.assertEqual(mid, get_operation(41)["canonical_message_id"])
        self.assertEqual([], list_chat_operations(9001))
        with db.get_connection() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        operation, _ = register_operation(chat_id=9001, client_operation_id="new-identical",
            text="old text", intent_origin="message", author_user_id=None, author_label="Employee")
        self.assertEqual("pending", operation["status"])

    def test_wrong_scope_direction_or_text_cannot_confirm(self):
        for index, values in enumerate((dict(chat_id=9002), dict(direction="inbound"),
                                       dict(text="another"), dict(external_id="other"))):
            mid = self.message(message_id=61+index, external_id=f"provider-{index}", **{
                key: value for key, value in values.items() if key != "external_id"})
            self.old_operation(operation_id=41+index, status="confirmed",
                external_id=values.get("external_id", f"provider-{index}"), canonical_id=mid)
        db.init_db()
        self.assertEqual(["uncertain"] * 4, [get_operation(41+i)["status"] for i in range(4)])

    def test_all_legacy_states_are_non_dispatchable_and_safe_publicly(self):
        states = ("pending", "sending", "accepted", "confirmed", "failed", "uncertain")
        for i, status in enumerate(states):
            self.old_operation(operation_id=41+i, status=status, text=f"text-{i}")
        db.init_db()
        connector = mock.Mock()
        connector.send_message = mock.AsyncMock()
        service = MessageSendService({"ozon": connector})

        async def exercise():
            for i in range(len(states)):
                op, dedup = await service.register_and_dispatch(chat_id=9001,
                    client_operation_id=f"old-{41+i}", text=f"text-{i}", intent_origin="message",
                    author_user_id=None, author_label="ignored")
                self.assertTrue(dedup)
                self.assertEqual("uncertain", op["status"])
                self.assertEqual("uncertain", (await service.dispatch_operation(41+i))["status"])
                rendered = json.dumps(public_operation(op))
                self.assertNotIn("private", rendered)
                self.assertNotIn("SPOOFED", rendered)
            self.assertEqual({"processed": 0, "stale_uncertain": 0}, await service.drain_once())

        asyncio.run(exercise())
        connector.normalize_text_command.assert_not_called()
        connector.send_message.assert_not_called()

    def test_highwater_includes_deleted_ids_and_no_lookup_collision(self):
        self.old_operation()
        self.old_operation(operation_id=200)
        with db.get_connection() as conn:
            conn.execute("DELETE FROM message_send_operations WHERE id=200")
        db.init_db()
        operation, _ = register_operation(chat_id=9001, client_operation_id="new",
            text="new text", intent_origin="message", author_user_id=None, author_label="Employee")
        self.assertGreater(operation["id"], 200)
        self.assertTrue(get_operation(41)["legacy_review"])
        self.assertNotIn("legacy_review", get_operation(operation["id"]))

    def test_identity_merge_remaps_effective_link_but_not_audit(self):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name='20260806_message_identity'")
            conn.execute("DROP INDEX IF EXISTS idx_messages_chat_external_unique")
        first = self.message(message_id=61, crm=1)
        second = self.message(message_id=62, crm=0, external_id="provider-2")
        # Duplicate operation identity is enough for the preserved identity migration.
        with db.get_connection() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_messages_chat_operation_unique")
            conn.execute("UPDATE messages SET client_operation_id='logical-operation'")
        self.old_operation(status="confirmed", external_id="provider-1", canonical_id=second)
        db.init_db()
        with db.get_connection() as conn:
            self.assertEqual(second, conn.execute("SELECT canonical_message_id FROM legacy_message_send_audit").fetchone()[0])
            self.assertEqual(first, conn.execute("SELECT canonical_message_id FROM legacy_message_review_links").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT id FROM messages WHERE id=?", (second,)).fetchone())

    def test_archive_is_immutable_and_survives_parent_deletion(self):
        self.old_operation()
        db.init_db()
        with db.get_connection() as conn:
            for sql in ("DELETE FROM legacy_message_send_audit", "UPDATE legacy_message_send_audit SET id=42",
                        "INSERT INTO legacy_message_send_audit SELECT * FROM legacy_message_send_audit",
                        "DELETE FROM legacy_hosting_audit_manifest"):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
            conn.execute("DELETE FROM chats WHERE id=9001")
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM legacy_message_send_audit").fetchone()[0])
        db.init_db()

    def test_missing_review_link_fails_before_modern_startup_writes(self):
        self.old_operation()
        db.init_db()
        with db.get_connection() as conn:
            conn.execute("DELETE FROM legacy_message_review_links")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
            db.init_db()
        self.assertEqual(before, self.snapshot())

    def test_trigger_with_same_name_but_no_guard_is_rejected(self):
        self.old_operation()
        db.init_db()
        with db.get_connection() as conn:
            conn.execute("DROP TRIGGER legacy_message_send_audit_no_update")
            conn.execute("CREATE TRIGGER legacy_message_send_audit_no_update BEFORE UPDATE ON legacy_message_send_audit BEGIN SELECT 1; END")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
            db.init_db()
        self.assertEqual(before, self.snapshot())

    def test_conversion_faults_roll_back_all_tables_and_markers(self):
        self.old_operation()
        before = self.snapshot()
        for name in ("_archive", "apply_message_send_operation_migration",
                     "apply_task_chat_status_automation_migration", "_validate_ledger"):
            original = getattr(upgrade, name)
            def fail_after(*args, _original=original):
                _original(*args)
                raise RuntimeError("synthetic fault")
            with self.subTest(stage=name), mock.patch.object(upgrade, name, side_effect=fail_after):
                with self.assertRaisesRegex(RuntimeError, "synthetic fault"):
                    db.init_db()
            self.assertEqual(before, self.snapshot())

    def test_concurrent_connections_cannot_bypass_unresolved_hash(self):
        self.old_operation()
        db.init_db()
        barrier = threading.Barrier(2)
        def register(index):
            barrier.wait(timeout=5)
            try:
                register_operation(chat_id=9001, client_operation_id=f"new-{index}",
                    text="old text", intent_origin="message", author_user_id=None, author_label="Employee")
            except MessageSendOperationConflict:
                return "conflict"
            return "unsafe"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(["conflict", "conflict"], list(pool.map(register, range(2))))
        self.assertIsNone(claim_next_operation())

    def test_chat_status_and_old_task_ownership_are_not_replayed(self):
        with db.get_connection() as conn:
            status = conn.execute("SELECT id FROM chat_statuses WHERE key='waiting_customer'").fetchone()[0]
            conn.execute("INSERT INTO task_types(id,title,is_active) VALUES(7001,'Dormant type',0)")
            conn.execute("INSERT INTO task_type_chat_status_links(task_type_id,chat_status_id) VALUES(7001,?)", (status,))
            conn.execute("""INSERT INTO tasks(id,chat_id,title,task_type_id,status,chat_status_effect_key,
                chat_status_effect_applied_at,chat_status_effect_cycle)
                VALUES(8001,9001,'Legacy task',7001,'open','waiting_customer','2026-01-01','legacy-cycle')""")
            conn.execute("""INSERT INTO chat_task_status_state(chat_id,baseline_status,active_task_id,
                active_status_key,cycle_id) VALUES(9001,'new',8001,'waiting_customer','legacy-cycle')""")
            chat_before = tuple(conn.execute("SELECT status,metadata_json,updated_at FROM chats WHERE id=9001").fetchone())
        db.init_db()
        with db.get_connection() as conn:
            self.assertEqual(chat_before, tuple(conn.execute("SELECT status,metadata_json,updated_at FROM chats WHERE id=9001").fetchone()))
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM legacy_task_status_audit").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM chat_task_status_state").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM task_chat_status_effects").fetchone()[0])
            self.assertEqual(status, conn.execute("SELECT chat_status_id FROM task_type_chat_status_links WHERE task_type_id=7001").fetchone()[0])
        repo.update_task_type(7001, TaskTypeUpdate(is_active=True))
        repo.update_task(8001, TaskUpdate(title="Unrelated edit"))
        self.assertEqual("closed", repo.get_chat_summary(9001)["status"])
        repo.update_task(8001, TaskUpdate(status="done"))
        self.assertEqual("closed", repo.get_chat_summary(9001)["status"])
        repo.update_task(8001, TaskUpdate(status="open"))
        self.assertEqual("waiting_customer", repo.get_chat_summary(9001)["status"])

    def test_uppercase_hybrid_does_not_bypass_preflight(self):
        with db.get_connection() as conn:
            conn.execute("ALTER TABLE message_send_operations RENAME COLUMN message_type TO MESSAGE_TYPE")
            conn.execute("ALTER TABLE chat_task_status_state RENAME COLUMN cycle_id TO CYCLE_ID")
            conn.execute("ALTER TABLE message_send_operations ADD COLUMN surprise TEXT")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
            db.init_db()
        self.assertEqual(before, self.snapshot())

    def test_check_literal_is_not_normalized_away(self):
        with db.get_connection() as conn:
            conn.execute("DROP TABLE message_send_operations")
            conn.executescript(LEGACY_DDL.replace("('text')", "(' text ')").replace("DEFAULT 'text'", "DEFAULT ' text '"))
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
            db.init_db()
        self.assertEqual(before, self.snapshot())

    def quote_all_table_identifiers(self):
        # A hosting database rebuilt through .recover/.dump or ALTER TABLE RENAME
        # stores CREATE TABLE "name" (...) and quoted references, same schema.
        with db.get_connection() as conn:
            names = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            for name in names:
                conn.execute(f'ALTER TABLE "{name}" RENAME TO "{name}__rebuilt"')
                conn.execute(f'ALTER TABLE "{name}__rebuilt" RENAME TO "{name}"')
            quoted = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE TABLE \"%'"
            )]
            outbox_sql = str(conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='message_send_operations'"
            ).fetchone()[0])
        self.assertEqual(sorted(names), sorted(quoted))
        self.assertIn('REFERENCES "chats"', outbox_sql)

    def test_rebuilt_database_with_quoted_identifiers_converts(self):
        self.old_operation()
        self.quote_all_table_identifiers()
        db.init_db()
        with db.get_connection() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name=?", (upgrade.MIGRATION,)
            ).fetchone())
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM legacy_message_send_audit").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM message_send_operations").fetchone()[0])
        self.assertEqual("uncertain", get_operation(41)["status"])
        self.assertIsNone(claim_next_operation(operation_id=41))
        db.init_db()
        self.assertEqual("uncertain", get_operation(41)["status"])

    def test_only_renamed_object_names_are_normalized(self):
        self.assertEqual(
            upgrade._sql('CREATE TABLE "message_send_operations" (id INTEGER, FOREIGN KEY(chat_id) REFERENCES "chats"(id))'),
            upgrade._sql("CREATE TABLE IF NOT EXISTS message_send_operations (id INTEGER, FOREIGN KEY(chat_id) REFERENCES chats(id))"),
        )
        self.assertEqual(
            upgrade._sql('CREATE INDEX idx_x ON "tasks"(chat_id) WHERE chat_status_effect_key IS NOT NULL'),
            upgrade._sql("CREATE INDEX IF NOT EXISTS idx_x ON tasks(chat_id) WHERE chat_status_effect_key IS NOT NULL"),
        )
        self.assertNotEqual(upgrade._sql('CHECK(message_type IN ("text"))'), upgrade._sql("CHECK(message_type IN ('text'))"))
        self.assertNotEqual(upgrade._sql('DEFAULT "current_timestamp"'), upgrade._sql("DEFAULT CURRENT_TIMESTAMP"))
        self.assertNotEqual(upgrade._sql('CREATE TABLE " messages "(id)'), upgrade._sql("CREATE TABLE messages(id)"))
        self.assertNotEqual(upgrade._sql('CREATE TABLE "my table"(id)'), upgrade._sql("CREATE TABLE mytable(id)"))
        self.assertNotEqual(upgrade._sql('CREATE TABLE "a""b"(id)'), upgrade._sql("CREATE TABLE ab(id)"))
        self.assertNotEqual(upgrade._sql("DEFAULT 'text'"), upgrade._sql('DEFAULT "text"'))

    def stray_effects_table(self, ddl=None):
        # What an interrupted newer startup leaves behind: table and indexes
        # committed by SQLite before the automation validators raised.
        with db.get_connection() as conn:
            conn.execute(ddl or automation.TASK_CHAT_STATUS_EFFECTS_SQL)
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_task_chat_effects_chat_cycle_active
                ON task_chat_status_effects(chat_id, cycle, released_at, applied_at DESC, id DESC)""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_task_chat_effects_one_active_task
                ON task_chat_status_effects(task_id) WHERE released_at IS NULL""")

    def test_empty_effects_table_left_by_interrupted_startup_is_rebuilt(self):
        self.old_operation()
        self.stray_effects_table()
        self.quote_all_table_identifiers()
        db.init_db()
        with db.get_connection() as conn:
            markers = {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}
            self.assertLessEqual({upgrade.MIGRATION, automation.MIGRATION_NAME}, markers)
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM legacy_message_send_audit").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM task_chat_status_effects").fetchone()[0])
            indexes = {str(r["name"]) for r in conn.execute("PRAGMA index_list(task_chat_status_effects)") if r["origin"] == "c"}
            self.assertEqual({"idx_task_chat_effects_chat_cycle_active", "idx_task_chat_effects_one_active_task"}, indexes)
        self.assertEqual("uncertain", get_operation(41)["status"])
        db.init_db()

    def test_used_or_different_effects_table_fails_closed(self):
        self.old_operation()

        def used():
            self.stray_effects_table()
            with db.get_connection() as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute("INSERT INTO task_chat_status_effects(task_id,chat_id,cycle,mapped_status_key) VALUES(1,9001,1,'new')")

        def emptied_after_use():
            used()
            with db.get_connection() as conn:
                conn.execute("DELETE FROM task_chat_status_effects")

        def different_ddl():
            self.stray_effects_table(automation.TASK_CHAT_STATUS_EFFECTS_SQL.replace(
                "release_reason TEXT,", "release_reason TEXT, surprise TEXT,"))

        def referenced_by_another_table():
            self.stray_effects_table()
            with db.get_connection() as conn:
                conn.execute("CREATE TABLE effect_notes(id INTEGER PRIMARY KEY, effect_id INTEGER REFERENCES task_chat_status_effects(id))")

        def used_by_a_view():
            self.stray_effects_table()
            with db.get_connection() as conn:
                conn.execute("CREATE VIEW effects_view AS SELECT id FROM task_chat_status_effects")

        def used_by_a_trigger_on_another_table():
            self.stray_effects_table()
            with db.get_connection() as conn:
                conn.execute("""CREATE TRIGGER tasks_touch_effects AFTER DELETE ON tasks
                    BEGIN DELETE FROM task_chat_status_effects WHERE task_id=OLD.id; END""")

        cases = (used, emptied_after_use, different_ddl, referenced_by_another_table,
                 used_by_a_view, used_by_a_trigger_on_another_table)
        for case in cases:
            with self.subTest(case=case.__name__):
                try:
                    case()
                    before = self.snapshot()
                    with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
                        db.init_db()
                    self.assertEqual(before, self.snapshot())
                finally:
                    with db.get_connection() as conn:
                        conn.execute("DROP TRIGGER IF EXISTS tasks_touch_effects")
                        conn.execute("DROP VIEW IF EXISTS effects_view")
                        conn.execute("DROP TABLE IF EXISTS effect_notes")
                        conn.execute("DROP TABLE IF EXISTS task_chat_status_effects")

    def test_failed_marker_insert_rolls_back_identity_and_archive(self):
        self.old_operation()
        self.stray_effects_table()  # dropped by the bridge; must come back on rollback
        with db.get_connection() as conn:
            conn.execute(f"""CREATE TRIGGER fail_upgrade_marker BEFORE INSERT ON schema_migrations
                WHEN NEW.name='{upgrade.MIGRATION}' BEGIN SELECT RAISE(ABORT,'synthetic marker fault'); END""")
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic marker fault"):
            db.init_db()
        self.assertEqual(before, self.snapshot())

    def test_missing_parent_scope_and_marker_fail_without_conversion(self):
        self.old_operation()
        for mutation in ("UPDATE message_send_operations SET external_chat_id='wrong-target'",
                         "DELETE FROM schema_migrations WHERE name='20260806_message_send_outbox'",
                         "DROP INDEX idx_message_send_operations_reconcile"):
            # Savepoint restores only this synthetic corruption between cases.
            with db.get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(mutation)
                before = tuple(conn.iterdump())
                with self.assertRaisesRegex(RuntimeError, "Unsupported legacy hosting schema"):
                    upgrade._preflight(conn)
                self.assertEqual(before, tuple(conn.iterdump()))
                conn.rollback()

    def test_deleted_mapping_parent_is_archived_not_restored(self):
        with db.get_connection() as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO task_type_chat_status_links(task_type_id,chat_status_id) VALUES(999,999)")
        db.init_db()
        with db.get_connection() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM legacy_task_mapping_audit").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM task_type_chat_status_links").fetchone()[0])

    def test_inactive_status_mapping_is_audited_without_reactivation(self):
        with db.get_connection() as conn:
            conn.execute("INSERT INTO task_types(id,title) VALUES(7001,'Legacy type')")
            conn.execute("INSERT INTO chat_statuses(id,key,title,is_active) VALUES(7002,'old-custom','Old custom',0)")
            conn.execute("INSERT INTO task_type_chat_status_links(task_type_id,chat_status_id) VALUES(7001,7002)")
        db.init_db()
        with db.get_connection() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM legacy_task_mapping_audit").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM task_type_chat_status_links").fetchone()[0])

    def test_concurrent_bridge_runs_archive_once(self):
        self.old_operation()
        barrier = threading.Barrier(2)
        def migrate(_):
            with db.get_connection() as conn:
                barrier.wait(timeout=5)
                return upgrade.apply_legacy_hosting_upgrade(conn, db._apply_message_identity_migration)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([False, True], sorted(pool.map(migrate, range(2))))
        db.init_db()
        self.assertEqual("uncertain", get_operation(41)["status"])

    def test_http_replay_poll_and_conflict_never_send_or_expose_audit(self):
        self.old_operation()
        with db.get_connection() as conn:
            conn.execute("UPDATE message_send_operations SET client_operation_id='legacy-41'")
        db.init_db()
        user = repo.create_user("review-manager", "synthetic-password", "Trusted employee", "admin")
        connector = mock.Mock()
        connector.send_message = mock.AsyncMock()
        service = MessageSendService({"ozon": connector})

        async def exercise():
            token = repo.create_session(user["id"], user_agent="synthetic-upgrade-test")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=foundation.main.app),
                                         base_url="https://testserver") as client:
                client.cookies.set(foundation.main.AUTH_COOKIE_NAME, token)
                csrf = await client.get("/api/security/csrf")
                headers = {foundation.main.CSRF_HEADER_NAME: csrf.json()["csrf_token"]}
                for _ in range(2):
                    response = await client.post("/api/chats/9001/messages", headers=headers,
                        json={"text": "old text", "operation_id": "legacy-41", "author": "Spoofed"})
                    self.assertEqual(202, response.status_code)
                    self.assertEqual(41, response.json()["operation"]["id"])
                    self.assertNotIn("private", response.text)
                    self.assertNotIn("SPOOFED", response.text)
                for op_id, text in (("legacy-41", "different"), ("new-id-123", "old text")):
                    response = await client.post("/api/chats/9001/messages", headers=headers,
                        json={"text": text, "operation_id": op_id})
                    self.assertEqual(409, response.status_code)
                listing = await client.get("/api/chats/9001/message-send-operations")
                detail = await client.get("/api/chats/9001/message-send-operations/legacy-41")
                self.assertEqual([41], [r["id"] for r in listing.json()["operations"]])
                self.assertEqual("uncertain", detail.json()["operation"]["status"])
                self.assertNotIn("private", detail.text + listing.text)

        with mock.patch.object(foundation.main, "message_send_service", service), mock.patch.object(
            foundation.main.app, "state", State()
        ):
            asyncio.run(exercise())
        connector.send_message.assert_not_called()

    def test_public_author_is_from_user_not_legacy_display_label(self):
        self.old_operation()
        with db.get_connection() as conn:
            conn.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES(401,'employee','Verified employee','synthetic-only')")
            conn.execute("UPDATE message_send_operations SET author_user_id=401")
        db.init_db()
        self.assertEqual("Verified employee", public_operation(get_operation(41))["author_label"])
        with db.get_connection() as conn:
            conn.execute("DELETE FROM users WHERE id=401")
        self.assertEqual("", public_operation(get_operation(41))["author_label"])

    def test_real_hosting_entry_points_initialize_and_serve_health(self):
        from a2wsgi import ASGIMiddleware
        self.old_operation()
        root = Path(__file__).resolve().parents[1]
        temp_site = Path(db.DATABASE_PATH).parent
        old_cwd = Path.cwd()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            def adapter(app):
                return ASGIMiddleware(app, loop=loop, wait_time=5)
            with mock.patch("a2wsgi.ASGIMiddleware", side_effect=adapter), mock.patch.dict(os.environ, {
                "DATABASE_PATH": db.DATABASE_PATH,
                "CRM_CHAT_ATTACHMENTS_DIR": str(temp_site / "attachments"),
            }):
                for entry in ("server.py", "passenger_wsgi.py"):
                    namespace = {"__name__": "synthetic_hosting_entry", "__file__": str(temp_site / entry)}
                    exec(compile((root / entry).read_text(encoding="utf-8"), str(temp_site / entry), "exec"), namespace)
                    self.assertIsInstance(namespace["application"], ASGIMiddleware)
                    environ = {}
                    setup_testing_defaults(environ)
                    environ["PATH_INFO"] = "/health"
                    environ["HTTP_HOST"] = "testserver"
                    response_status = []
                    response = namespace["application"](environ, lambda status, headers, exc_info=None: response_status.append(status))
                    try:
                        body = b"".join(response)
                    finally:
                        if hasattr(response, "close"):
                            response.close()
                    self.assertEqual(["200 OK"], response_status)
                    self.assertEqual({"status": "ok"}, json.loads(body))
                    self.assertEqual("uncertain", get_operation(41)["status"])
        finally:
            os.chdir(old_cwd)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            loop.close()

    def test_asgi_startup_shutdown_uses_shared_migration_without_legacy_send(self):
        self.old_operation()
        main = foundation.main
        async def idle_loop():
            await asyncio.Event().wait()
        async def exercise():
            await main.on_startup()
            try:
                self.assertEqual("uncertain", get_operation(41)["status"])
                self.assertIsNone(claim_next_operation())
            finally:
                await main.on_shutdown()
        with mock.patch.object(main.app, "state", State()), mock.patch.object(
            main, "_background_sync_loop", idle_loop
        ), mock.patch.object(main, "_wb_events_import_planner_loop", idle_loop), mock.patch.object(
            main, "_push_outbox_loop", idle_loop
        ), mock.patch.object(main, "_message_send_outbox_loop", idle_loop):
            asyncio.run(exercise())

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import test_regression_foundation as foundation
from app import db
from app import repository as repo
from app.schemas import (
    ChatCreate,
    ChatUpdate,
    TaskCreate,
    TaskTypeCreate,
    TaskTypeUpdate,
    TaskUpdate,
)
from app.task_chat_status_automation import (
    MIGRATION_NAME,
    apply_task_chat_status_automation_migration,
)


class TaskTypeChatStatusAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        foundation.main.app.state.security_rate_limits = {}
        db.init_db()
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id=f"task-status-{self._testMethodName}",
                status="new",
                metadata={"synthetic": True},
            )
        )

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def _status(self, title: str, key: str) -> dict:
        return repo.create_chat_status(title=title, key=key)

    def _task_type(self, title: str, chat_status_id: int | None) -> dict:
        return repo.create_task_type(
            TaskTypeCreate(title=title, chat_status_id=chat_status_id)
        )

    def _create_task(self, task_type_id: int | None) -> int:
        return repo.create_task(
            TaskCreate(
                chat_id=self.chat_id,
                title="Проверить обращение",
                task_type_id=task_type_id,
            )
        )

    def _chat_status(self) -> str:
        chat = repo.get_chat_summary(self.chat_id)
        assert chat is not None
        return str(chat["status"])

    def _state(self) -> dict | None:
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM chat_task_status_state WHERE chat_id=?", (self.chat_id,)
            ).fetchone()
            return dict(row) if row else None

    def _active_effects(self) -> list[dict]:
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_chat_status_effects
                WHERE chat_id=? AND released_at IS NULL
                ORDER BY applied_at, task_id, id
                """,
                (self.chat_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _provider_reopen_snapshot(self) -> dict:
        with db.get_connection() as conn:
            chat = conn.execute(
                "SELECT status, metadata_json, updated_at FROM chats WHERE id=?",
                (self.chat_id,),
            ).fetchone()
            state = conn.execute(
                "SELECT * FROM chat_task_status_state WHERE chat_id=?",
                (self.chat_id,),
            ).fetchone()
            effects = conn.execute(
                "SELECT * FROM task_chat_status_effects WHERE chat_id=? ORDER BY id",
                (self.chat_id,),
            ).fetchall()
        return {
            "chat": dict(chat) if chat else None,
            "state": dict(state) if state else None,
            "effects": [dict(row) for row in effects],
        }

    def test_create_with_mapping_applies_status_and_terminal_restores_baseline(self) -> None:
        target = self._status("На проверке", "under_review")
        task_type = self._task_type("Проверка", int(target["id"]))

        task_id = self._create_task(int(task_type["id"]))
        self.assertEqual("under_review", self._chat_status())

        for terminal in ("done", "archived", "cancelled"):
            with self.subTest(terminal=terminal):
                if terminal != "done":
                    repo.update_task(task_id, TaskUpdate(status="new"))
                    self.assertEqual("under_review", self._chat_status())
                repo.update_task(task_id, TaskUpdate(status=terminal))
                self.assertEqual("new", self._chat_status())

    def test_create_without_mapping_and_later_mapping_change_are_nonretroactive(self) -> None:
        target = self._status("Эскалация", "escalated")
        task_type = self._task_type("Без автоматики", None)
        task_id = self._create_task(int(task_type["id"]))

        repo.update_task_type(
            int(task_type["id"]),
            __import__("app.schemas", fromlist=["TaskTypeUpdate"]).TaskTypeUpdate(
                chat_status_id=int(target["id"])
            ),
        )
        repo.update_task(task_id, TaskUpdate(description="unrelated"))

        self.assertEqual("new", self._chat_status())

    def test_task_type_deactivation_preserves_dormant_mapping_and_releases_effects(self) -> None:
        target = self._status("Ожидание", "dormant_mapping")
        task_type = self._task_type("Ожидание", int(target["id"]))
        task_id = self._create_task(int(task_type["id"]))
        self.assertEqual("dormant_mapping", self._chat_status())

        updated = repo.update_task_type(
            int(task_type["id"]),
            TaskTypeUpdate(is_active=False, chat_status_id=int(target["id"])),
        )

        self.assertIsNotNone(updated)
        self.assertEqual(0, int(updated["is_active"]))
        self.assertEqual(int(target["id"]), int(updated["chat_status_id"]))
        self.assertEqual("new", self._chat_status())
        self.assertEqual([], self._active_effects())
        reloaded = next(
            item for item in repo.list_task_types(True) if int(item["id"]) == int(task_type["id"])
        )
        self.assertEqual(int(target["id"]), int(reloaded["chat_status_id"]))

        repo.update_task(task_id, TaskUpdate(status="done"))
        repo.update_task(task_id, TaskUpdate(status="new"))
        self.assertEqual("new", self._chat_status())
        self.assertEqual([], self._active_effects())

        repo.update_task_type(int(task_type["id"]), TaskTypeUpdate(is_active=True))
        self.assertEqual("new", self._chat_status())
        self.assertEqual([], self._active_effects())

        repo.update_task(task_id, TaskUpdate(status="done"))
        repo.update_task(task_id, TaskUpdate(status="new"))
        self.assertEqual("dormant_mapping", self._chat_status())
        self.assertEqual(1, len(self._active_effects()))

    def test_manual_override_invalidates_ownership_and_terminal_does_not_restore(self) -> None:
        target = self._status("На проверке", "under_review")
        manual = self._status("Ручной", "manual_hold")
        task_type = self._task_type("Проверка", int(target["id"]))
        task_id = self._create_task(int(task_type["id"]))

        repo.update_chat(self.chat_id, ChatUpdate(status=str(manual["key"])))
        repo.update_task(task_id, TaskUpdate(status="done"))

        self.assertEqual("manual_hold", self._chat_status())

    def test_latest_effect_wins_and_releasing_it_reveals_previous_effect(self) -> None:
        first_status = self._status("Первый", "first_task")
        second_status = self._status("Второй", "second_task")
        first_type = self._task_type("Первый", int(first_status["id"]))
        second_type = self._task_type("Второй", int(second_status["id"]))

        first_task = self._create_task(int(first_type["id"]))
        second_task = self._create_task(int(second_type["id"]))
        self.assertEqual("second_task", self._chat_status())

        repo.update_task(second_task, TaskUpdate(status="archived"))
        self.assertEqual("first_task", self._chat_status())
        repo.delete_task(first_task)
        self.assertEqual("new", self._chat_status())

    def test_provider_reopen_invalidates_task_owned_closed_status_idempotently(self) -> None:
        closed = next(
            status for status in repo.get_chat_settings()["statuses"] if status["key"] == "closed"
        )
        task_type = self._task_type("Закрывающая", int(closed["id"]))
        task_id = self._create_task(int(task_type["id"]))
        self.assertEqual("closed", self._chat_status())

        self.assertTrue(repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound"))
        provider_state = self._state()
        provider_cycle = int(provider_state["cycle"])
        self.assertEqual("provider", provider_state["override_kind"])
        before_repeat = self._provider_reopen_snapshot()
        self.assertFalse(repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound"))
        self.assertEqual(before_repeat, self._provider_reopen_snapshot())
        self.assertEqual(provider_cycle, int(self._state()["cycle"]))
        repo.update_task(task_id, TaskUpdate(status="done"))

        self.assertEqual("new", self._chat_status())

    def test_provider_reopen_accepts_only_inbound_for_literal_closed(self) -> None:
        repo.update_chat(self.chat_id, ChatUpdate(status="closed"))

        self.assertTrue(repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound"))
        self.assertEqual("new", self._chat_status())
        state = self._state()
        self.assertIsNotNone(state)
        self.assertEqual("provider", state["override_kind"])

    def test_provider_reopen_rejects_non_inbound_without_mutating_task_state(self) -> None:
        closed = next(
            status for status in repo.get_chat_settings()["statuses"] if status["key"] == "closed"
        )
        task_type = self._task_type("Закрывающая", int(closed["id"]))
        self._create_task(int(task_type["id"]))
        self.assertEqual("closed", self._chat_status())

        for direction in ("outbound", "internal", "", "unknown", None):
            with self.subTest(direction=direction):
                before = self._provider_reopen_snapshot()
                self.assertFalse(
                    repo.reopen_closed_chat_for_new_activity(self.chat_id, direction)
                )
                self.assertEqual(before, self._provider_reopen_snapshot())

    def test_provider_reopen_rejects_closed_like_status_keys_and_titles(self) -> None:
        variants = (
            ("archive", "Archive"),
            ("archived", "Archived"),
            ("zakryt", "Zakryt"),
            ("zakryto", "Zakryto"),
            ("закрыт", "Русский ключ"),
            ("custom_closed_title", "Закрыт"),
            ("not_closed_status", "Contains closed in key"),
        )
        with db.get_connection() as conn:
            for key, title in variants:
                conn.execute(
                    """
                    INSERT INTO chat_statuses(
                        key, title, color, sort_order, is_system, is_active
                    ) VALUES (?, ?, 'gray', 500, 0, 1)
                    """,
                    (key, title),
                )

        for key, _title in variants:
            with self.subTest(status_key=key):
                with db.get_connection() as conn:
                    cursor = conn.execute(
                        "UPDATE chats SET status=? WHERE id=?",
                        (key, self.chat_id),
                    )
                    self.assertEqual(1, cursor.rowcount)
                before = self._provider_reopen_snapshot()
                self.assertFalse(
                    repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound")
                )
                self.assertEqual(before, self._provider_reopen_snapshot())

    def test_provider_reopen_is_noop_for_already_open_chat(self) -> None:
        before = self._provider_reopen_snapshot()

        self.assertFalse(repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound"))
        self.assertEqual(before, self._provider_reopen_snapshot())

    def test_explicit_task_event_after_provider_override_starts_new_cycle(self) -> None:
        closed = next(item for item in repo.get_chat_settings()["statuses"] if item["key"] == "closed")
        closing = self._task_type("Закрывающий", int(closed["id"]))
        first_task = self._create_task(int(closing["id"]))
        self.assertTrue(repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound"))
        overridden_cycle = int(self._state()["cycle"])

        second_task = self._create_task(int(closing["id"]))
        current = self._state()
        self.assertGreater(int(current["cycle"]), overridden_cycle)
        self.assertEqual("closed", self._chat_status())
        self.assertEqual(second_task, int(current["active_task_id"]))

        repo.update_task(first_task, TaskUpdate(status="done"))
        self.assertEqual("closed", self._chat_status())

    def test_type_rebind_releases_old_snapshot_and_unrelated_update_does_not_reacquire(self) -> None:
        first = self._status("Первый", "type_first")
        second = self._status("Второй", "type_second")
        first_type = self._task_type("Первый тип", int(first["id"]))
        second_type = self._task_type("Второй тип", int(second["id"]))
        task_id = self._create_task(int(first_type["id"]))

        repo.update_task(task_id, TaskUpdate(task_type_id=int(second_type["id"])))
        effects_after_rebind = self._active_effects()
        self.assertEqual("type_second", self._chat_status())
        self.assertEqual([task_id], [int(item["task_id"]) for item in effects_after_rebind])
        effect_id = int(effects_after_rebind[0]["id"])

        repo.update_task(task_id, TaskUpdate(description="Только описание"))
        self.assertEqual(effect_id, int(self._active_effects()[0]["id"]))

    def test_reactivation_starts_a_new_cycle_and_delete_releases_it(self) -> None:
        status = self._status("Ожидание", "reactivation_status")
        task_type = self._task_type("Реактивация", int(status["id"]))
        task_id = self._create_task(int(task_type["id"]))
        first_cycle = int(self._state()["cycle"])

        repo.update_task(task_id, TaskUpdate(status="done"))
        repo.update_task(task_id, TaskUpdate(status="new"))
        second_cycle = int(self._state()["cycle"])

        self.assertGreater(second_cycle, first_cycle)
        self.assertEqual("reactivation_status", self._chat_status())
        self.assertTrue(repo.delete_task(task_id))
        self.assertEqual("new", self._chat_status())
        self.assertEqual([], self._active_effects())

    def test_invalid_or_inactive_mapping_is_rejected_atomically(self) -> None:
        with self.assertRaises(ValueError):
            self._task_type("Некорректный", 999999)
        self.assertFalse(any(item["title"] == "Некорректный" for item in repo.list_task_types(True)))

        target = self._status("Отключённый", "inactive_mapping")
        repo.update_chat_status(int(target["id"]), {"is_active": False})
        with self.assertRaises(ValueError):
            self._task_type("Неактивный", int(target["id"]))

    def test_deleted_or_inactive_baseline_is_not_restored(self) -> None:
        baseline = self._status("Временный", "temporary_baseline")
        target = self._status("Эффект", "effect_status")
        repo.update_chat(self.chat_id, ChatUpdate(status=str(baseline["key"])))
        task_type = self._task_type("Эффект", int(target["id"]))
        task_id = self._create_task(int(task_type["id"]))

        self.assertTrue(repo.delete_chat_status(int(baseline["id"])))
        repo.update_task(task_id, TaskUpdate(status="done"))

        self.assertEqual("new", self._chat_status())

    def test_deactivating_mapped_status_releases_multiple_chats_and_clears_mapping(self) -> None:
        target = self._status("Отключаемый", "deactivate_effect")
        task_type = self._task_type("Отключаемый", int(target["id"]))
        second_chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id=f"second-{self._testMethodName}",
                status="new",
                metadata={"synthetic": True},
            )
        )
        first_task = self._create_task(int(task_type["id"]))
        second_task = repo.create_task(
            TaskCreate(chat_id=second_chat_id, title="Вторая", task_type_id=int(task_type["id"]))
        )

        updated = repo.update_chat_status(int(target["id"]), {"is_active": False})

        self.assertIsNotNone(updated)
        self.assertEqual(0, int(updated["is_active"]))
        self.assertEqual("new", self._chat_status())
        self.assertEqual("new", str(repo.get_chat_summary(second_chat_id)["status"]))
        self.assertEqual([], self._active_effects())
        with db.get_connection() as conn:
            self.assertEqual(
                0,
                int(
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM task_chat_status_effects WHERE task_id IN (?, ?) AND released_at IS NULL",
                        (first_task, second_task),
                    ).fetchone()["c"]
                ),
            )
        reloaded = next(item for item in repo.list_task_types(True) if item["id"] == task_type["id"])
        self.assertIsNone(reloaded["chat_status_id"])

        repo.update_chat_status(int(target["id"]), {"is_active": True})
        reloaded = next(item for item in repo.list_task_types(True) if item["id"] == task_type["id"])
        self.assertIsNone(reloaded["chat_status_id"])
        self.assertEqual([], self._active_effects())
        self.assertEqual("new", self._chat_status())
        self.assertEqual("new", str(repo.get_chat_summary(second_chat_id)["status"]))

    def test_deleting_mapped_status_preserves_override_and_does_not_restore_mapping(self) -> None:
        target = self._status("Удаляемый", "delete_effect")
        manual = self._status("Ручной", "delete_manual")
        task_type = self._task_type("Удаляемый", int(target["id"]))
        task_id = self._create_task(int(task_type["id"]))
        repo.update_chat(self.chat_id, ChatUpdate(status=str(manual["key"])))
        second_chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id=f"delete-second-{self._testMethodName}",
                status="new",
                metadata={"synthetic": True},
            )
        )
        second_task = repo.create_task(
            TaskCreate(chat_id=second_chat_id, title="Активный effect", task_type_id=int(task_type["id"]))
        )

        self.assertTrue(repo.delete_chat_status(int(target["id"])))
        self.assertEqual("delete_manual", self._chat_status())
        self.assertEqual("new", str(repo.get_chat_summary(second_chat_id)["status"]))
        self.assertEqual("manual", self._state()["override_kind"])
        self.assertEqual([], self._active_effects())
        with db.get_connection() as conn:
            active_second = conn.execute(
                "SELECT COUNT(*) AS c FROM task_chat_status_effects WHERE task_id=? AND released_at IS NULL",
                (second_task,),
            ).fetchone()["c"]
        self.assertEqual(0, int(active_second))
        mapping = next(item for item in repo.list_task_types(True) if item["id"] == task_type["id"])
        self.assertIsNone(mapping["chat_status_id"])

        repo.update_task(task_id, TaskUpdate(description="unrelated"))
        self.assertEqual("delete_manual", self._chat_status())

    def test_status_deactivation_preserves_provider_override_and_clears_mapping(self) -> None:
        closed = next(item for item in repo.get_chat_settings()["statuses"] if item["key"] == "closed")
        task_type = self._task_type("Закрывающий", int(closed["id"]))
        self._create_task(int(task_type["id"]))
        self.assertTrue(repo.reopen_closed_chat_for_new_activity(self.chat_id, "inbound"))
        self.assertEqual("provider", self._state()["override_kind"])

        repo.update_chat_status(int(closed["id"]), {"is_active": False})

        self.assertEqual("new", self._chat_status())
        self.assertEqual("provider", self._state()["override_kind"])
        mapping = next(item for item in repo.list_task_types(True) if item["id"] == task_type["id"])
        self.assertIsNone(mapping["chat_status_id"])

    def test_chat_status_deactivation_rolls_back_all_chats_on_recalculation_error(self) -> None:
        target = self._status("Откат", "status_rollback")
        task_type = self._task_type("Откат", int(target["id"]))
        self._create_task(int(task_type["id"]))
        second_chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id=f"rollback-second-{self._testMethodName}",
                status="new",
                metadata={"synthetic": True},
            )
        )
        repo.create_task(
            TaskCreate(chat_id=second_chat_id, title="Вторая", task_type_id=int(task_type["id"]))
        )
        from app import task_chat_status_automation as automation

        original_recalculate = automation.recalculate_chat_status_conn
        calls = 0

        def fail_on_second_chat(conn, chat_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("boom")
            return original_recalculate(conn, chat_id, **kwargs)

        with mock.patch.object(automation, "recalculate_chat_status_conn", side_effect=fail_on_second_chat):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                repo.update_chat_status(int(target["id"]), {"is_active": False})

        status = next(item for item in repo.get_chat_settings()["statuses"] if item["id"] == target["id"])
        self.assertEqual(1, int(status["is_active"]))
        self.assertEqual("status_rollback", self._chat_status())
        self.assertEqual("status_rollback", str(repo.get_chat_summary(second_chat_id)["status"]))
        self.assertEqual(1, len(self._active_effects()))
        mapping = next(item for item in repo.list_task_types(True) if item["id"] == task_type["id"])
        self.assertEqual(int(target["id"]), int(mapping["chat_status_id"]))

    def test_chat_status_deactivation_without_safe_fallback_rolls_back(self) -> None:
        target = self._status("Единственный", "only_active")
        task_type = self._task_type("Единственный", int(target["id"]))
        self._create_task(int(task_type["id"]))
        with db.get_connection() as conn:
            conn.execute("UPDATE chat_statuses SET is_active=0 WHERE id<>?", (int(target["id"]),))

        with self.assertRaisesRegex(RuntimeError, "No active chat status"):
            repo.update_chat_status(int(target["id"]), {"is_active": False})

        status = next(item for item in repo.get_chat_settings()["statuses"] if item["id"] == target["id"])
        self.assertEqual(1, int(status["is_active"]))
        self.assertEqual("only_active", self._chat_status())
        self.assertEqual(1, len(self._active_effects()))

    def test_forward_reconcile_and_rollback_release_are_idempotent(self) -> None:
        target = self._status("Откат", "rollback_effect")
        task_type = self._task_type("Откат", int(target["id"]))
        self._create_task(int(task_type["id"]))

        repo.reconcile_task_chat_status_automation()
        repo.reconcile_task_chat_status_automation()
        self.assertEqual("rollback_effect", self._chat_status())
        self.assertEqual(1, repo.release_task_chat_status_automation_for_rollback())
        self.assertEqual(0, repo.release_task_chat_status_automation_for_rollback())
        self.assertEqual("new", self._chat_status())

    def test_task_and_effect_roll_back_together_on_arbiter_error(self) -> None:
        target = self._status("Ошибка", "rollback_on_error")
        task_type = self._task_type("Ошибка", int(target["id"]))
        with mock.patch.object(repo, "acquire_task_effect_conn", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self._create_task(int(task_type["id"]))
        with db.get_connection() as conn:
            self.assertEqual(
                0,
                int(conn.execute("SELECT COUNT(*) AS c FROM tasks WHERE chat_id=?", (self.chat_id,)).fetchone()["c"]),
            )

    def test_concurrent_type_changes_keep_task_effect_state_and_chat_consistent(self) -> None:
        first = self._status("Конкурентный A", "concurrent_a")
        second = self._status("Конкурентный B", "concurrent_b")
        first_type = self._task_type("Тип A", int(first["id"]))
        second_type = self._task_type("Тип B", int(second["id"]))
        task_id = self._create_task(int(first_type["id"]))

        def change(type_id: int) -> None:
            repo.update_task(task_id, TaskUpdate(task_type_id=type_id))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(change, (int(first_type["id"]), int(second_type["id"]))))

        task = repo.get_task(task_id)
        assert task is not None
        expected = {
            int(first_type["id"]): "concurrent_a",
            int(second_type["id"]): "concurrent_b",
        }[int(task["task_type_id"])]
        state = self._state()
        self.assertEqual(expected, self._chat_status())
        self.assertEqual(expected, state["active_status_key"])
        self.assertEqual(task_id, int(state["active_task_id"]))
        self.assertEqual(1, len(self._active_effects()))

    def test_concurrent_update_delete_leaves_no_orphan_effect(self) -> None:
        first = self._status("Удаление A", "delete_a")
        second = self._status("Удаление B", "delete_b")
        first_type = self._task_type("Удаление A", int(first["id"]))
        second_type = self._task_type("Удаление B", int(second["id"]))
        task_id = self._create_task(int(first_type["id"]))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(repo.update_task, task_id, TaskUpdate(task_type_id=int(second_type["id"]))),
                pool.submit(repo.delete_task, task_id),
            ]
            for future in futures:
                future.result()

        self.assertIsNone(repo.get_task(task_id))
        self.assertEqual([], self._active_effects())
        self.assertEqual("new", self._chat_status())

    def test_concurrent_terminal_rebind_and_manual_update_remain_atomic(self) -> None:
        first = self._status("Гонка A", "race_a")
        second = self._status("Гонка B", "race_b")
        manual = self._status("Ручной итог", "race_manual")
        first_type = self._task_type("Гонка A", int(first["id"]))
        second_type = self._task_type("Гонка B", int(second["id"]))
        task_id = self._create_task(int(first_type["id"]))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(repo.update_task, task_id, TaskUpdate(status="done")),
                pool.submit(repo.update_task, task_id, TaskUpdate(task_type_id=int(second_type["id"]))),
            ]
            for future in futures:
                future.result()
        self.assertEqual([], self._active_effects())
        self.assertEqual("new", self._chat_status())

        repo.update_task(task_id, TaskUpdate(status="new"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(repo.update_chat, self.chat_id, ChatUpdate(status=str(manual["key"]))),
                pool.submit(repo.update_task, task_id, TaskUpdate(task_type_id=int(first_type["id"]))),
            ]
            for future in futures:
                future.result()
        state = self._state()
        if state["override_kind"] == "manual":
            self.assertEqual("race_manual", self._chat_status())
            self.assertEqual([], self._active_effects())
        else:
            self.assertIsNone(state["override_kind"])
            self.assertEqual("race_a", self._chat_status())
            self.assertEqual(1, len(self._active_effects()))

    def test_provider_reopen_racing_task_rebind_has_one_consistent_winner(self) -> None:
        closed = next(item for item in repo.get_chat_settings()["statuses"] if item["key"] == "closed")
        unmapped = self._task_type("Без статуса", None)
        closing = self._task_type("Закрывающий", int(closed["id"]))
        task_id = self._create_task(int(unmapped["id"]))
        repo.update_chat(self.chat_id, ChatUpdate(status="closed"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(repo.update_task, task_id, TaskUpdate(task_type_id=int(closing["id"]))),
                pool.submit(repo.reopen_closed_chat_for_new_activity, self.chat_id, "inbound"),
            ]
            for future in futures:
                future.result()

        state = self._state()
        active = self._active_effects()
        if self._chat_status() == "closed":
            self.assertEqual(1, len(active))
            self.assertIsNone(state["override_kind"])
            self.assertEqual("closed", state["active_status_key"])
        else:
            self.assertEqual("new", self._chat_status())
            self.assertEqual([], active)
            self.assertEqual("provider", state["override_kind"])

    def test_migration_is_idempotent_cycle_indexed_and_nonretroactive(self) -> None:
        task_type = self._task_type("Без эффекта до mapping", None)
        task_id = self._create_task(int(task_type["id"]))
        target = self._status("Поздний mapping", "late_mapping")
        repo.update_task_type(
            int(task_type["id"]),
            __import__("app.schemas", fromlist=["TaskTypeUpdate"]).TaskTypeUpdate(
                chat_status_id=int(target["id"])
            ),
        )

        db.init_db()
        db.init_db()
        with db.get_connection() as conn:
            marker_count = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)
            ).fetchone()["c"]
            effect_count = conn.execute(
                "SELECT COUNT(*) AS c FROM task_chat_status_effects WHERE task_id=?", (task_id,)
            ).fetchone()["c"]
            index_columns = [
                tuple(item["name"] for item in conn.execute(f"PRAGMA index_info({row['name']})"))
                for row in conn.execute("PRAGMA index_list(task_chat_status_effects)")
            ]
        self.assertEqual(1, int(marker_count))
        self.assertEqual(0, int(effect_count))
        self.assertIn(
            ("chat_id", "cycle", "released_at", "applied_at", "id"),
            index_columns,
        )
        self.assertEqual("new", self._chat_status())


class TaskChatStatusMigrationContractTests(unittest.TestCase):
    def tearDown(self) -> None:
        foundation._remove_test_runtime_files()

    def _base_connection(self):
        foundation._remove_test_runtime_files()
        conn = foundation._REAL_SQLITE_CONNECT(str(foundation._DATABASE_PATH))
        conn.row_factory = db.sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE schema_migrations(
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE chats(id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE task_types(id INTEGER PRIMARY KEY, is_active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE chat_statuses(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                task_type_id INTEGER, status TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id),
                FOREIGN KEY(task_type_id) REFERENCES task_types(id));
            """
        )
        return conn

    def test_compatible_partial_schema_is_completed(self) -> None:
        conn = self._base_connection()
        try:
            conn.execute(
                """
                CREATE TABLE task_type_chat_status_links(
                    task_type_id INTEGER PRIMARY KEY,
                    chat_status_id INTEGER NOT NULL,
                    FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE RESTRICT
                )
                """
            )
            apply_task_chat_status_automation_migration(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_type_chat_status_links)")}
            self.assertTrue({"created_at", "updated_at"}.issubset(columns))
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()

    def test_incompatible_partial_schema_fails_before_marker(self) -> None:
        conn = self._base_connection()
        try:
            conn.execute(
                """
                CREATE TABLE task_type_chat_status_links(
                    task_type_id INTEGER PRIMARY KEY,
                    chat_status_id TEXT NOT NULL,
                    FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE RESTRICT
                )
                """
            )
            with self.assertRaisesRegex(RuntimeError, "chat_status_id type"):
                apply_task_chat_status_automation_migration(conn)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()

    def test_nonempty_effect_table_without_applied_at_fails_closed(self) -> None:
        conn = self._base_connection()
        try:
            conn.executescript(
                """
                INSERT INTO chats(id, status) VALUES (1, 'new');
                INSERT INTO task_types(id, is_active) VALUES (1, 1);
                INSERT INTO chat_statuses(id, key, is_active, sort_order)
                VALUES (1, 'new', 1, 0);
                INSERT INTO tasks(id, chat_id, task_type_id, status)
                VALUES (1, 1, 1, 'new');
                CREATE TABLE task_chat_status_effects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    cycle INTEGER NOT NULL CHECK(cycle > 0),
                    chat_status_id INTEGER,
                    mapped_status_key TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT,
                    UNIQUE(task_id, cycle),
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE SET NULL
                );
                INSERT INTO task_chat_status_effects(
                    task_id, chat_id, cycle, chat_status_id, mapped_status_key
                ) VALUES (1, 1, 1, 1, 'new');
                """
            )
            with self.assertRaisesRegex(RuntimeError, "missing trustworthy audit columns"):
                apply_task_chat_status_automation_migration(conn)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()

    def test_wrong_foreign_key_action_fails_before_marker(self) -> None:
        conn = self._base_connection()
        try:
            conn.execute(
                """
                CREATE TABLE task_type_chat_status_links(
                    task_type_id INTEGER PRIMARY KEY,
                    chat_status_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE CASCADE
                )
                """
            )
            with self.assertRaisesRegex(RuntimeError, "foreign keys"):
                apply_task_chat_status_automation_migration(conn)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()

    def test_wrong_audit_nullability_or_default_fails_before_marker(self) -> None:
        conn = self._base_connection()
        try:
            conn.execute(
                """
                CREATE TABLE task_type_chat_status_links(
                    task_type_id INTEGER PRIMARY KEY,
                    chat_status_id INTEGER NOT NULL,
                    created_at TEXT DEFAULT 'legacy',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE RESTRICT
                )
                """
            )
            with self.assertRaisesRegex(RuntimeError, "created_at (nullability|default)"):
                apply_task_chat_status_automation_migration(conn)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()

    def test_unrelated_foreign_key_violation_does_not_block_migration(self) -> None:
        conn = self._base_connection()
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(
                """
                CREATE TABLE legacy_messages(
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chats(id)
                );
                INSERT INTO legacy_messages(id, chat_id) VALUES (1, 999);
                """
            )
            conn.execute("PRAGMA foreign_keys=ON")

            self.assertTrue(conn.execute("PRAGMA foreign_key_check").fetchall())
            apply_task_chat_status_automation_migration(conn)

            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
            self.assertTrue(
                conn.execute("PRAGMA foreign_key_check(legacy_messages)").fetchall()
            )
        finally:
            conn.close()

    def test_automation_foreign_key_violation_still_fails_closed(self) -> None:
        conn = self._base_connection()
        try:
            apply_task_chat_status_automation_migration(conn)
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                """
                INSERT INTO task_type_chat_status_links(task_type_id, chat_status_id)
                VALUES (999, 999)
                """
            )
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")

            with self.assertRaisesRegex(RuntimeError, "foreign-key data"):
                apply_task_chat_status_automation_migration(conn)
            self.assertTrue(
                conn.execute(
                    "PRAGMA foreign_key_check(task_type_chat_status_links)"
                ).fetchall()
            )
        finally:
            conn.close()

    def test_cycle_index_mismatch_is_rejected_even_with_marker(self) -> None:
        conn = self._base_connection()
        try:
            apply_task_chat_status_automation_migration(conn)
            conn.execute("DROP INDEX idx_task_chat_effects_chat_cycle_active")
            conn.execute(
                """
                CREATE INDEX idx_task_chat_effects_chat_cycle_active
                ON task_chat_status_effects(chat_id, released_at, cycle, applied_at, id)
                """
            )
            with self.assertRaisesRegex(RuntimeError, "idx_task_chat_effects_chat_cycle_active"):
                apply_task_chat_status_automation_migration(conn)
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()

    def test_empty_partial_schema_with_malformed_index_fails_before_rebuild(self) -> None:
        conn = self._base_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE task_type_chat_status_links(
                    task_type_id INTEGER PRIMARY KEY,
                    chat_status_id INTEGER NOT NULL,
                    FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE CASCADE,
                    FOREIGN KEY(chat_status_id) REFERENCES chat_statuses(id) ON DELETE RESTRICT
                );
                CREATE INDEX idx_task_type_chat_status_links_status
                ON task_type_chat_status_links(task_type_id);
                """
            )
            with self.assertRaisesRegex(
                RuntimeError, "idx_task_type_chat_status_links_status definition"
            ):
                apply_task_chat_status_automation_migration(conn)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(task_type_chat_status_links)")
            }
            self.assertNotIn("created_at", columns)
            self.assertIsNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (MIGRATION_NAME,)).fetchone()
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

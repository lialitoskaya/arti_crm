from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import httpx

import test_regression_foundation as foundation
from app import db
from app import repository as repo
from app.schemas import ChatCreate, TaskCreate, TaskUpdate


main = foundation.main


class TaskAssigneeDateSortTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.manager_a = repo.create_user(
            "task-date-manager-a",
            "task-date-manager-a-password",
            "Анна",
            "manager",
        )
        self.manager_b = repo.create_user(
            "task-date-manager-b",
            "task-date-manager-b-password",
            "Борис",
            "manager",
        )
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="task-date-chat",
                customer_name="Task Date Customer",
                metadata={"synthetic": True},
            )
        )
        self.later_id = repo.create_task(
            TaskCreate(
                chat_id=self.chat_id,
                title="Поздняя задача",
                assigned_user_id=int(self.manager_a["id"]),
                due_at="2026-08-10T12:00:00",
            )
        )
        self.earlier_id = repo.create_task(
            TaskCreate(
                chat_id=self.chat_id,
                title="Ранняя задача",
                assigned_user_id=int(self.manager_b["id"]),
                due_at="2026-08-08T09:30:00",
            )
        )
        self.no_date_id = repo.create_task(
            TaskCreate(
                chat_id=self.chat_id,
                title="Без даты",
                assigned_user_id=int(self.manager_b["id"]),
                due_at=None,
            )
        )
        repo.update_task(self.earlier_id, TaskUpdate(status="archived"))

        # This row proves that due-date filtering does not fall back to created_at.
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET created_at=? WHERE id=?",
                ("2026-08-08T15:00:00Z", self.no_date_id),
            )

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_task_filter_indexes_exist(self) -> None:
        with db.get_connection() as conn:
            names = {row["name"] for row in conn.execute("PRAGMA index_list(tasks)").fetchall()}
        self.assertIn("idx_tasks_due_at_id", names)
        self.assertIn("idx_tasks_assigned_due_id", names)

    def test_repository_sorts_strictly_by_due_date_with_undated_last(self) -> None:
        tasks = repo.list_tasks()
        self.assertEqual(
            [self.later_id, self.earlier_id, self.no_date_id],
            [int(task["id"]) for task in tasks],
        )

        chat = repo.get_chat(self.chat_id, current_user_id=int(self.manager_a["id"]))
        self.assertIsNotNone(chat)
        self.assertEqual(
            [self.later_id, self.earlier_id, self.no_date_id],
            [int(task["id"]) for task in chat["tasks"]],
        )

    def test_repository_filters_by_due_date_only(self) -> None:
        tasks = repo.list_tasks(due_date="2026-08-08")
        self.assertEqual([self.earlier_id], [int(task["id"]) for task in tasks])

    def test_api_filters_by_explicit_assignee(self) -> None:
        token = repo.create_session(int(self.manager_a["id"]), user_agent="task-filter-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get(
                    "/api/tasks",
                    params={"assigned_user_id": int(self.manager_b["id"])},
                )

        response = asyncio.run(exercise())
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [self.earlier_id, self.no_date_id],
            [int(task["id"]) for task in response.json()],
        )

    def test_mine_filter_has_precedence_over_explicit_assignee(self) -> None:
        token = repo.create_session(int(self.manager_a["id"]), user_agent="task-filter-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get(
                    "/api/tasks",
                    params={
                        "mine": "true",
                        "assigned_user_id": int(self.manager_b["id"]),
                    },
                )

        response = asyncio.run(exercise())
        self.assertEqual(200, response.status_code)
        self.assertEqual([self.later_id], [int(task["id"]) for task in response.json()])


class TaskAssigneeDateSortUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        cls.styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    def test_task_filters_are_server_backed_and_include_assignee(self) -> None:
        self.assertIn('id="taskAssigneeFilter"', self.html)
        self.assertIn("function buildTaskListQuery()", self.source)
        self.assertIn("params.set('assigned_user_id', assignedUserId)", self.source)
        self.assertIn("params.set('due_date', dueDate)", self.source)
        self.assertIn("params.set('task_type_id', taskTypeId)", self.source)
        self.assertIn("params.set('q', searchValue)", self.source)
        self.assertNotIn("function filterTasksForUi", self.source)
        self.assertNotIn("function taskMatchesDate", self.source)
        self.assertNotIn("window.lastLoadedTasks", self.source)

    def test_task_card_renders_canonical_due_date_field(self) -> None:
        self.assertIn('<span class="tasks-ref-field-title">Дата</span>', self.source)
        self.assertIn('datetime="${escapeHtml(task.due_at || \'\')}"', self.source)
        self.assertIn("const dueLabel = formatDateTime(task.due_at) || 'Без даты';", self.source)


    def test_task_actions_are_compact_icon_only_controls(self) -> None:
        self.assertIn('aria-label="Редактировать задачу"', self.source)
        self.assertIn('aria-label="Удалить задачу"', self.source)
        self.assertIn('aria-label="Открыть чат"', self.source)
        self.assertNotIn('title="Редактировать задачу">✎</button>', self.source)
        self.assertNotIn('title="Удалить задачу">×</button>', self.source)
        self.assertNotIn('class="tasks-ref-chat-btn" type="button" data-open-chat', self.source)
        self.assertIn('#tasksView .tasks-ref-icon-btn {', self.styles)
        self.assertIn('border: 0;', self.styles)
        self.assertIn('#tasksView .tasks-ref-chat-btn {', self.styles)
        self.assertIn('width: 26px;', self.styles)


if __name__ == "__main__":
    unittest.main()

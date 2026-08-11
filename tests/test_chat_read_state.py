from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


main = foundation.main


async def _client_for_user(user: dict[str, object]) -> httpx.AsyncClient:
    token = repo.create_session(int(user["id"]), user_agent="chat-read-state-test")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
    )
    client.cookies.set(main.AUTH_COOKIE_NAME, token)
    return client


async def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain CSRF token: {response.status_code}")
    return {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


class ChatReadStateTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.manager_a = repo.create_user(
            "read-manager-a",
            "read-manager-a-password",
            "Manager A",
            "manager",
        )
        self.manager_b = repo.create_user(
            "read-manager-b",
            "read-manager-b-password",
            "Manager B",
            "manager",
        )
        self.viewer = repo.create_user(
            "read-viewer",
            "read-viewer-password",
            "Read Viewer",
            "viewer",
        )
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="read-state-chat",
                customer_name="Synthetic Customer",
                metadata={},
            )
        )
        self._message_counter = 0

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def _add_message(self, direction: str, text: str) -> int:
        self._message_counter += 1
        return repo.add_message(
            self.chat_id,
            direction,
            text,
            author="synthetic-customer" if direction == "inbound" else "Manager A",
            external_message_id=f"read-state-message-{self._message_counter}",
            raw={"synthetic": True},
        )

    def _state(self, user: dict[str, object]) -> dict[str, object]:
        state = repo.get_chat_read_state(self.chat_id, int(user["id"]))
        self.assertIsNotNone(state)
        return state

    def test_new_inbound_message_makes_chat_unread_for_each_active_user(self) -> None:
        message_id = self._add_message("inbound", "new inbound")

        for user in (self.manager_a, self.manager_b, self.viewer):
            with self.subTest(role=user["role"], user_id=user["id"]):
                state = self._state(user)
                self.assertTrue(state["is_unread"])
                self.assertEqual(message_id, state["unread_message_id"])

        listed = repo.list_chats(current_user_id=int(self.viewer["id"]))
        self.assertEqual(1, len(listed))
        self.assertTrue(listed[0]["is_unread"])

    def test_outbound_message_does_not_make_read_chat_unread(self) -> None:
        self._add_message("inbound", "needs attention")
        repo.set_chat_read_state(self.chat_id, int(self.manager_a["id"]), is_unread=False)

        self._add_message("outbound", "employee reply")

        self.assertFalse(self._state(self.manager_a)["is_unread"])

    def test_replayed_historical_inbound_does_not_reopen_a_read_chat(self) -> None:
        self._add_message("inbound", "current inbound")
        repo.set_chat_read_state(self.chat_id, int(self.manager_a["id"]), is_unread=False)

        repo.add_message(
            self.chat_id,
            "inbound",
            "historical replay",
            author="synthetic-customer",
            external_message_id="historical-read-state-replay",
            raw={"synthetic": True},
            created_at="2020-01-01T00:00:00+00:00",
        )

        self.assertFalse(self._state(self.manager_a)["is_unread"])
        self.assertTrue(self._state(self.manager_b)["is_unread"])

    def test_employee_reply_does_not_mark_another_employee_read(self) -> None:
        self._add_message("inbound", "question")
        repo.set_chat_read_state(self.chat_id, int(self.manager_a["id"]), is_unread=False)

        self._add_message("outbound", "reply by A")

        self.assertFalse(self._state(self.manager_a)["is_unread"])
        self.assertTrue(self._state(self.manager_b)["is_unread"])

    def test_opening_marks_only_current_user_read(self) -> None:
        self._add_message("inbound", "personal boundary")

        canonical = repo.set_chat_read_state(
            self.chat_id,
            int(self.manager_a["id"]),
            is_unread=False,
        )

        self.assertFalse(canonical["is_unread"])
        self.assertFalse(self._state(self.manager_a)["is_unread"])
        self.assertTrue(self._state(self.manager_b)["is_unread"])

    def test_manual_unread_is_personal(self) -> None:
        marked = repo.set_chat_read_state(
            self.chat_id,
            int(self.manager_a["id"]),
            is_unread=True,
        )

        self.assertTrue(marked["is_unread"])
        self.assertTrue(marked["is_marked_unread"])
        self.assertFalse(self._state(self.manager_b)["is_unread"])

    def test_repeated_patch_is_idempotent_and_viewer_can_mutate_own_state(self) -> None:
        self._add_message("inbound", "viewer message")

        async def exercise():
            async with await _client_for_user(self.viewer) as client:
                headers = await _csrf_headers(client)
                first = await client.patch(
                    f"/api/chats/{self.chat_id}/read-state",
                    json={"is_unread": False},
                    headers=headers,
                )
                second = await client.patch(
                    f"/api/chats/{self.chat_id}/read-state",
                    json={"is_unread": False},
                    headers=headers,
                )
                return first, second

        with (
            mock.patch.object(main, "_sync_marketplace_locked") as marketplace_sync,
            mock.patch.object(main, "_sync_ozon_fast_inbox_locked") as fast_sync,
            mock.patch.object(main, "_run_background_tick_once") as background_tick,
        ):
            first, second = asyncio.run(exercise())

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.chat_id, first.json()["chat_id"])
        self.assertFalse(first.json()["is_unread"])
        self.assertTrue(self._state(self.manager_a)["is_unread"])
        marketplace_sync.assert_not_called()
        fast_sync.assert_not_called()
        background_tick.assert_not_called()

    def test_unauthenticated_patch_returns_401(self) -> None:
        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                return await client.patch(
                    f"/api/chats/{self.chat_id}/read-state",
                    json={"is_unread": False},
                )

        response = asyncio.run(exercise())
        self.assertEqual(401, response.status_code)

    def test_authenticated_patch_without_csrf_is_rejected(self) -> None:
        async def exercise():
            async with await _client_for_user(self.viewer) as client:
                return await client.patch(
                    f"/api/chats/{self.chat_id}/read-state",
                    json={"is_unread": False},
                )

        response = asyncio.run(exercise())
        self.assertEqual(403, response.status_code)
        self.assertIn("CSRF", response.json()["detail"])

    def test_old_history_stays_read_after_idempotent_migration(self) -> None:
        repo.add_message(
            self.chat_id,
            "inbound",
            "historical message",
            author="synthetic-customer",
            external_message_id="historical-read-state-message",
            raw={"synthetic": True},
            created_at="2020-01-01T00:00:00+00:00",
        )
        with db.get_connection() as conn:
            conn.execute("DROP TABLE chat_user_states")

        db.init_db()
        db.init_db()

        state = self._state(self.manager_a)
        self.assertFalse(state["is_unread"])
        with db.get_connection() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(chat_user_states)").fetchall()
            }
            row_count = conn.execute(
                "SELECT COUNT(*) AS count FROM chat_user_states"
            ).fetchone()["count"]
        self.assertEqual(
            {
                "user_id",
                "chat_id",
                "last_read_message_id",
                "last_read_at",
                "is_marked_unread",
                "is_pinned",
                "pinned_at",
                "updated_at",
            },
            columns,
        )
        self.assertEqual(0, int(row_count))

    def test_sqlite_is_restricted_to_the_foundation_temp_database(self) -> None:
        self.assertEqual(
            Path(foundation._DATABASE_PATH).resolve(),
            Path(db.DATABASE_PATH).resolve(),
        )


if __name__ == "__main__":
    unittest.main()

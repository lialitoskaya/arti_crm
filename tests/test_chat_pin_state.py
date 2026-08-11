from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


main = foundation.main


async def _client_for_user(user: dict[str, object]) -> httpx.AsyncClient:
    token = repo.create_session(int(user["id"]), user_agent="chat-pin-state-test")
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


class ChatPinStateTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.manager_a = repo.create_user(
            "pin-manager-a",
            "pin-manager-a-password",
            "Pin Manager A",
            "manager",
        )
        self.manager_b = repo.create_user(
            "pin-manager-b",
            "pin-manager-b-password",
            "Pin Manager B",
            "manager",
        )
        self.viewer = repo.create_user(
            "pin-viewer",
            "pin-viewer-password",
            "Pin Viewer",
            "viewer",
        )
        now = datetime.now(timezone.utc)
        self.older_chat_id = self._create_chat(
            "pin-older",
            "Older",
            now - timedelta(minutes=10),
        )
        self.newer_chat_id = self._create_chat(
            "pin-newer",
            "Newer",
            now - timedelta(minutes=1),
        )

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def _create_chat(self, external_id: str, customer_name: str, created_at: datetime) -> int:
        chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id=external_id,
                customer_name=customer_name,
                metadata={"synthetic": True},
            )
        )
        repo.add_message(
            chat_id,
            "inbound",
            customer_name,
            author="synthetic-customer",
            external_message_id=f"message-{external_id}",
            raw={"synthetic": True},
            created_at=created_at.isoformat(),
        )
        return chat_id

    def test_pin_is_personal_and_pinned_chats_sort_first(self) -> None:
        state = repo.set_chat_pin_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
            is_pinned=True,
        )

        self.assertTrue(state["is_pinned"])
        self.assertIsNotNone(state["pinned_at"])

        manager_a_list = repo.list_chats(current_user_id=int(self.manager_a["id"]))
        manager_b_list = repo.list_chats(current_user_id=int(self.manager_b["id"]))

        self.assertEqual(self.older_chat_id, manager_a_list[0]["id"])
        self.assertTrue(manager_a_list[0]["is_pinned"])
        self.assertEqual(self.newer_chat_id, manager_b_list[0]["id"])
        self.assertFalse(manager_b_list[0]["is_pinned"])

    def test_pinning_legacy_chat_does_not_make_old_history_unread(self) -> None:
        with db.get_connection() as conn:
            conn.execute(
                "DELETE FROM chat_user_states WHERE user_id=? AND chat_id=?",
                (int(self.manager_a["id"]), int(self.older_chat_id)),
            )

        before = repo.get_chat_read_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
        )
        self.assertFalse(before["is_unread"])
        self.assertIsNone(before["last_read_message_id"])

        repo.set_chat_pin_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
            is_pinned=True,
        )

        after = repo.get_chat_read_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
        )
        self.assertFalse(after["is_unread"])
        self.assertIsNotNone(after["last_read_message_id"])

    def test_repeated_pin_is_idempotent_and_unpin_preserves_read_state(self) -> None:
        repo.set_chat_read_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
            is_unread=False,
        )
        first = repo.set_chat_pin_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
            is_pinned=True,
        )
        second = repo.set_chat_pin_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
            is_pinned=True,
        )
        unpinned = repo.set_chat_pin_state(
            self.older_chat_id,
            int(self.manager_a["id"]),
            is_pinned=False,
        )

        self.assertEqual(first, second)
        self.assertFalse(unpinned["is_pinned"])
        self.assertIsNone(unpinned["pinned_at"])
        self.assertFalse(
            repo.get_chat_read_state(self.older_chat_id, int(self.manager_a["id"]))["is_unread"]
        )

    def test_viewer_can_patch_only_own_pin_state_with_csrf(self) -> None:
        async def exercise():
            async with await _client_for_user(self.viewer) as client:
                headers = await _csrf_headers(client)
                first = await client.patch(
                    f"/api/chats/{self.older_chat_id}/pin-state",
                    json={"is_pinned": True},
                    headers=headers,
                )
                second = await client.patch(
                    f"/api/chats/{self.older_chat_id}/pin-state",
                    json={"is_pinned": True},
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
        self.assertEqual(first.json(), second.json())
        self.assertTrue(first.json()["is_pinned"])
        self.assertFalse(
            repo.get_chat_pin_state(self.older_chat_id, int(self.manager_a["id"]))["is_pinned"]
        )
        marketplace_sync.assert_not_called()
        fast_sync.assert_not_called()
        background_tick.assert_not_called()

    def test_patch_requires_authentication_and_csrf(self) -> None:
        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as anonymous:
                unauthenticated = await anonymous.patch(
                    f"/api/chats/{self.older_chat_id}/pin-state",
                    json={"is_pinned": True},
                )
            async with await _client_for_user(self.viewer) as authenticated:
                without_csrf = await authenticated.patch(
                    f"/api/chats/{self.older_chat_id}/pin-state",
                    json={"is_pinned": True},
                )
            return unauthenticated, without_csrf

        unauthenticated, without_csrf = asyncio.run(exercise())
        self.assertEqual(401, unauthenticated.status_code)
        self.assertEqual(403, without_csrf.status_code)

    def test_existing_database_migration_adds_pin_columns_idempotently(self) -> None:
        with db.get_connection() as conn:
            conn.execute("ALTER TABLE chat_user_states RENAME TO chat_user_states_current")
            conn.execute(
                """
                CREATE TABLE chat_user_states (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    last_read_message_id INTEGER,
                    last_read_at TEXT,
                    is_marked_unread INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(user_id, chat_id)
                )
                """
            )
            conn.execute("DROP TABLE chat_user_states_current")

        db.init_db()
        db.init_db()

        with db.get_connection() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(chat_user_states)").fetchall()
            }
        self.assertIn("is_pinned", columns)
        self.assertIn("pinned_at", columns)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


main = foundation.main


async def _client_for_user(user: dict[str, object]) -> tuple[httpx.AsyncClient, dict[str, str]]:
    token = repo.create_session(int(user["id"]), user_agent="message-identity-test")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
    )
    client.cookies.set(main.AUTH_COOKIE_NAME, token)
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain CSRF token: {response.status_code}")
    headers = {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}
    return client, headers


class CrmMessageIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.user = repo.create_user(
            "message-identity-manager",
            "message-identity-password",
            "Лия",
            "admin",
        )
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="message-identity-chat",
                customer_name="Synthetic Customer",
                metadata={"source": "mock"},
            )
        )

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def _rows(self) -> list[dict[str, object]]:
        with db.get_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM messages WHERE chat_id=? ORDER BY id",
                    (self.chat_id,),
                ).fetchall()
            ]

    def test_crm_row_and_later_provider_echo_become_one_message_with_author(self) -> None:
        local_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Одинаковый ответ",
            author="Лия",
            raw={"_crm_send_ack_message_id": "send-ack-1"},
            created_at="2026-08-06T08:00:00+00:00",
            is_crm_sent=True,
            crm_author_user_id=int(self.user["id"]),
            crm_author_label="Лия",
            client_operation_id="operation-local-first",
        )
        echo_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Одинаковый ответ",
            author="seller",
            external_message_id="history-message-1",
            raw={"message_id": "history-message-1", "user": {"type": "seller"}},
            created_at="2026-08-06T08:00:02+00:00",
        )

        self.assertEqual(local_id, echo_id)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("history-message-1", rows[0]["external_message_id"])
        self.assertEqual(1, rows[0]["is_crm_sent"])
        self.assertEqual("Лия", rows[0]["crm_author_label"])
        self.assertEqual(int(self.user["id"]), rows[0]["crm_author_user_id"])

        chat = repo.get_chat(self.chat_id)
        self.assertEqual("Лия", chat["messages"][0]["crm_author_label"])

    def test_provider_echo_arriving_before_local_save_is_enriched_not_duplicated(self) -> None:
        provider_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Ответ в обратной гонке",
            author="seller",
            external_message_id="history-message-2",
            raw={"message_id": "history-message-2", "user": {"type": "seller"}},
            created_at="2026-08-06T08:10:00+00:00",
        )
        local_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Ответ в обратной гонке",
            author="Лия",
            raw={"_crm_send_ack_message_id": "send-ack-2"},
            created_at="2026-08-06T08:10:01+00:00",
            is_crm_sent=True,
            crm_author_user_id=int(self.user["id"]),
            crm_author_label="Лия",
            client_operation_id="operation-provider-first",
        )

        self.assertEqual(provider_id, local_id)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("history-message-2", rows[0]["external_message_id"])
        self.assertEqual("Лия", rows[0]["crm_author_label"])
        self.assertEqual("operation-provider-first", rows[0]["client_operation_id"])

    def test_same_client_operation_is_idempotent(self) -> None:
        first = repo.add_message(
            self.chat_id,
            "outbound",
            "Один запрос",
            author="Лия",
            created_at="2026-08-06T08:20:00+00:00",
            is_crm_sent=True,
            crm_author_user_id=int(self.user["id"]),
            crm_author_label="Лия",
            client_operation_id="operation-idempotent",
        )
        second = repo.add_message(
            self.chat_id,
            "outbound",
            "Один запрос",
            author="Лия",
            created_at="2026-08-06T08:20:01+00:00",
            is_crm_sent=True,
            crm_author_user_id=int(self.user["id"]),
            crm_author_label="Лия",
            client_operation_id="operation-idempotent",
        )

        self.assertEqual(first, second)
        self.assertEqual(1, len(self._rows()))

    def test_api_repeated_operation_does_not_send_to_marketplace_twice(self) -> None:
        async def exercise():
            client, headers = await _client_for_user(self.user)
            try:
                payload = {
                    "text": "API idempotency",
                    "author": "manager",
                    "operation_id": "operation-api-idempotent",
                }
                first = await client.post(f"/api/chats/{self.chat_id}/messages", json=payload, headers=headers)
                second = await client.post(f"/api/chats/{self.chat_id}/messages", json=payload, headers=headers)
                return first, second
            finally:
                await client.aclose()

        with mock.patch.object(
            main.connectors["mock"],
            "send_message",
            new=mock.AsyncMock(return_value={"message_id": "send-ack-api"}),
        ) as send_message:
            first, second = asyncio.run(exercise())

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json()["message_id"], second.json()["message_id"])
        self.assertTrue(second.json()["deduplicated"])
        send_message.assert_awaited_once()
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("Лия", rows[0]["crm_author_label"])
        self.assertEqual("operation-api-idempotent", rows[0]["client_operation_id"])
        raw = json.loads(str(rows[0]["raw_json"]))
        self.assertEqual("send-ack-api", raw["_crm_send_ack_message_id"])

    def test_migration_repairs_existing_crm_provider_pair_once(self) -> None:
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    chat_id, external_message_id, direction, author, text, created_at,
                    raw_json, is_crm_sent, crm_author_user_id, crm_author_label,
                    client_operation_id
                ) VALUES (?, ?, 'outbound', ?, ?, ?, ?, 0, NULL, NULL, NULL)
                """,
                (
                    self.chat_id,
                    "old-send-ack",
                    "Лия",
                    "Старый дубль",
                    "2026-08-06T08:30:00+00:00",
                    json.dumps(
                        {
                            "_crm_sent_from_crm": True,
                            "_crm_sent_by_label": "Лия",
                            "_crm_sent_by_user_id": int(self.user["id"]),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO messages (
                    chat_id, external_message_id, direction, author, text, created_at,
                    raw_json, is_crm_sent, crm_author_user_id, crm_author_label,
                    client_operation_id
                ) VALUES (?, ?, 'outbound', 'seller', ?, ?, ?, 0, NULL, NULL, NULL)
                """,
                (
                    self.chat_id,
                    "old-history-id",
                    "Старый дубль",
                    "2026-08-06T08:30:01+00:00",
                    json.dumps({"message_id": "old-history-id"}),
                ),
            )
            conn.execute(
                "DELETE FROM schema_migrations WHERE name='20260806_message_identity'"
            )

        db.init_db()
        db.init_db()

        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("old-history-id", rows[0]["external_message_id"])
        self.assertEqual(1, rows[0]["is_crm_sent"])
        self.assertEqual("Лия", rows[0]["crm_author_label"])
        raw = json.loads(str(rows[0]["raw_json"]))
        self.assertEqual("old-send-ack", raw["_crm_send_ack_message_id"])
        with db.get_connection() as conn:
            migration_count = conn.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations WHERE name='20260806_message_identity'"
            ).fetchone()["count"]
        self.assertEqual(1, migration_count)

    def test_seller_echo_misclassified_as_inbound_keeps_crm_message_outbound(self) -> None:
        local_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Ответ без ложного unread",
            author="Лия",
            created_at="2026-08-06T08:40:00+00:00",
            is_crm_sent=True,
            crm_author_user_id=int(self.user["id"]),
            crm_author_label="Лия",
            client_operation_id="operation-inbound-echo",
        )
        echo_id = repo.add_message(
            self.chat_id,
            "inbound",
            "Ответ без ложного unread",
            author="customer",
            external_message_id="history-message-inbound-echo",
            raw={"message_id": "history-message-inbound-echo", "user": {"type": "seller"}},
            created_at="2026-08-06T08:40:02+00:00",
        )

        self.assertEqual(local_id, echo_id)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("outbound", rows[0]["direction"])
        self.assertEqual("Лия", rows[0]["crm_author_label"])
        with db.get_connection() as conn:
            unread_rows = conn.execute(
                "SELECT COUNT(*) AS count FROM chat_user_states WHERE chat_id=? AND is_marked_unread=1",
                (self.chat_id,),
            ).fetchone()["count"]
        self.assertEqual(0, unread_rows)

    def test_ambiguous_identical_replies_are_not_merged_by_guesswork(self) -> None:
        for suffix, created_at in (("one", "2026-08-06T08:50:00+00:00"), ("two", "2026-08-06T08:50:04+00:00")):
            repo.add_message(
                self.chat_id,
                "outbound",
                "Повторяемый ответ",
                author="Лия",
                created_at=created_at,
                is_crm_sent=True,
                crm_author_user_id=int(self.user["id"]),
                crm_author_label="Лия",
                client_operation_id=f"operation-ambiguous-{suffix}",
            )

        repo.add_message(
            self.chat_id,
            "outbound",
            "Повторяемый ответ",
            author="seller",
            external_message_id="history-ambiguous",
            raw={"message_id": "history-ambiguous", "user": {"type": "seller"}},
            created_at="2026-08-06T08:50:02+00:00",
        )

        self.assertEqual(3, len(self._rows()))

    def test_migration_resolves_author_from_structured_user_identity(self) -> None:
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    chat_id, external_message_id, direction, author, text, created_at,
                    raw_json, is_crm_sent, crm_author_user_id, crm_author_label,
                    client_operation_id
                ) VALUES (?, ?, 'outbound', 'manager', ?, ?, ?, 0, NULL, NULL, NULL)
                """,
                (
                    self.chat_id,
                    "old-user-only",
                    "Старое сообщение с user id",
                    "2026-08-06T09:00:00+00:00",
                    json.dumps(
                        {
                            "_crm_sent_from_crm": True,
                            "_crm_sent_by_user_id": int(self.user["id"]),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            conn.execute("DELETE FROM schema_migrations WHERE name='20260806_message_identity'")

        db.init_db()

        row = next(item for item in self._rows() if item["external_message_id"] == "old-user-only")
        self.assertEqual(1, row["is_crm_sent"])
        self.assertEqual(int(self.user["id"]), row["crm_author_user_id"])
        self.assertEqual("Лия", row["crm_author_label"])
        self.assertEqual("Лия", row["author"])

    def test_identity_unique_indexes_exist(self) -> None:
        with db.get_connection() as conn:
            indexes = {
                row["name"]: bool(row["unique"])
                for row in conn.execute("PRAGMA index_list(messages)").fetchall()
            }
        self.assertTrue(indexes.get("idx_messages_chat_external_unique"))
        self.assertTrue(indexes.get("idx_messages_chat_operation_unique"))


if __name__ == "__main__":
    unittest.main()

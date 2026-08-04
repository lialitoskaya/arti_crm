from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


_IMPORT_TEMP_DIRECTORY = tempfile.TemporaryDirectory(prefix="arti_crm_yandex_import_")
_IMPORT_DATABASE_PATH = str(Path(_IMPORT_TEMP_DIRECTORY.name) / "import.sqlite3")
_SAFE_IMPORT_ENV = {
    "APP_ENV": "test",
    "DATABASE_PATH": _IMPORT_DATABASE_PATH,
    "CRM_AUTH_DISABLED": "0",
    "CRM_FORCE_HTTPS": "0",
    "WB_EVENTS_AUTO_IMPORT_ENABLED": "false",
    "WEB_PUSH_ENABLED": "false",
    "PYTHON_DOTENV_DISABLED": "1",
    "OZON_CLIENT_ID": "",
    "OZON_API_KEY": "",
    "WB_BUYERS_CHAT_TOKEN": "",
    "YANDEX_MARKET_TOKEN": "",
    "YANDEX_MARKET_BUSINESS_ID": "",
    "OPENAI_API_KEY": "",
}


def _deny_network(*_args, **_kwargs):
    raise AssertionError("Network access is forbidden in Yandex chat sync tests")


_REAL_SOCKET = socket.socket


class _NoNetworkSocket(_REAL_SOCKET):
    def connect(self, *_args, **_kwargs):
        return _deny_network()

    def connect_ex(self, *_args, **_kwargs):
        return _deny_network()


import dotenv  # noqa: E402  (must be patched before app.db is imported)

with (
    patch.dict(os.environ, _SAFE_IMPORT_ENV, clear=True),
    patch.object(dotenv, "load_dotenv", return_value=False),
    patch.object(socket, "socket", _NoNetworkSocket),
    patch.object(socket, "create_connection", _deny_network),
):
    from app import db  # noqa: E402
    from app import main  # noqa: E402
    from app import repository as repo  # noqa: E402
    from app.connectors.base import UnifiedChat, UnifiedMessage  # noqa: E402
    from app.connectors.yandex_market import (  # noqa: E402
        YandexChatHistoryContractError,
        YandexChatHistoryFetchError,
        YandexMarketConnector,
    )
    from app.schemas import ChatCreate  # noqa: E402


def _chat(external_id: str, *, provider_status: str = "NEW") -> UnifiedChat:
    return UnifiedChat(
        marketplace="yandex",
        external_chat_id=external_id,
        customer_name="Test customer",
        status="new" if provider_status in {"NEW", "WAITING_FOR_PARTNER"} else "waiting_customer",
        metadata={
            "chatId": external_id,
            "status": provider_status,
            "_sync_hint": {
                "yandex_chat_status": provider_status,
                "yandex_needs_partner_reply": provider_status in {"NEW", "WAITING_FOR_PARTNER"},
            },
        },
    )


def _message(
    external_chat_id: str,
    external_message_id: str,
    *,
    direction: str = "inbound",
    created_at: str = "2026-01-01T10:00:00Z",
    text: str = "Anonymized message",
) -> UnifiedMessage:
    return UnifiedMessage(
        external_message_id=external_message_id,
        external_chat_id=external_chat_id,
        direction=direction,
        text=text,
        author="customer" if direction == "inbound" else "seller",
        created_at=created_at,
        raw={"messageId": external_message_id},
    )


class FakeYandexConnector:
    marketplace = "yandex"

    def __init__(self, chats: list[UnifiedChat], histories: dict[str, object]) -> None:
        self.chats = chats
        self.histories = histories
        self.history_calls: list[str] = []

    async def list_chats(self) -> list[UnifiedChat]:
        return self.chats

    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        self.history_calls.append(external_chat_id)
        result = self.histories[external_chat_id]
        if isinstance(result, BaseException):
            raise result
        return list(result)  # type: ignore[arg-type]


class YandexChatSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = str(Path(self.temp_dir.name) / "test.sqlite3")
        db.init_db()

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.old_database_path
        self.temp_dir.cleanup()

    def _sync(self, connector: FakeYandexConnector) -> dict[str, object]:
        with patch.dict(main.connectors, {"yandex": connector}):
            return asyncio.run(main._sync_marketplace_unlocked("yandex"))

    def _create_chat(self, chat: UnifiedChat) -> int:
        return repo.upsert_chat(
            ChatCreate(
                marketplace="yandex",
                external_chat_id=chat.external_chat_id,
                customer_name=chat.customer_name,
                status=chat.status,  # type: ignore[arg-type]
                metadata=chat.metadata,
            )
        )

    def _set_updated_at(self, chat_id: int, value: str) -> None:
        with db.get_connection() as conn:
            conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (value, chat_id))

    def _updated_at(self, chat_id: int) -> str:
        with db.get_connection() as conn:
            return str(conn.execute("SELECT updated_at FROM chats WHERE id=?", (chat_id,)).fetchone()["updated_at"])

    def test_new_chat_with_valid_empty_history_is_not_created(self) -> None:
        connector = FakeYandexConnector([_chat("new-empty")], {"new-empty": []})

        result = self._sync(connector)

        self.assertTrue(result["ok"])
        self.assertIsNone(repo.get_chat_by_external("yandex", "new-empty"))
        self.assertEqual(["new-empty"], connector.history_calls)

    def test_history_timeout_or_error_does_not_create_shell(self) -> None:
        connector = FakeYandexConnector(
            [_chat("new-timeout"), _chat("new-error")],
            {
                "new-timeout": TimeoutError("history timeout"),
                "new-error": RuntimeError("provider error"),
            },
        )

        result = self._sync(connector)

        self.assertFalse(result["ok"])
        self.assertEqual(2, result["errors_count"])
        self.assertIsNone(repo.get_chat_by_external("yandex", "new-timeout"))
        self.assertIsNone(repo.get_chat_by_external("yandex", "new-error"))
        self.assertCountEqual(["new-timeout", "new-error"], connector.history_calls)

    def test_connector_accepts_explicit_empty_messages_list(self) -> None:
        connector = YandexMarketConnector.__new__(YandexMarketConnector)
        connector.token = "test-token"
        connector.business_id = "test-business"
        connector._post = AsyncMock(return_value={"result": {"messages": []}})  # type: ignore[method-assign]

        messages = asyncio.run(connector.get_messages("chat-empty"))

        self.assertEqual([], messages)
        connector._post.assert_awaited_once()

    def test_connector_history_error_does_not_expose_provider_payload(self) -> None:
        connector = YandexMarketConnector.__new__(YandexMarketConnector)
        connector.token = "test-token"
        connector.business_id = "test-business"
        connector._post = AsyncMock(side_effect=RuntimeError("private provider payload"))  # type: ignore[method-assign]

        with self.assertRaises(YandexChatHistoryFetchError) as raised:
            asyncio.run(connector.get_messages("chat-error"))

        self.assertEqual("Yandex chat history request failed", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_missing_or_malformed_messages_is_contract_error_not_empty(self) -> None:
        connector = YandexMarketConnector.__new__(YandexMarketConnector)
        connector.token = "test-token"
        connector.business_id = "test-business"

        for payload in ({"result": {}}, {"result": {"messages": {}}}, {"result": {"messages": [None]}}):
            connector._post = AsyncMock(return_value=payload)  # type: ignore[method-assign]
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(YandexChatHistoryContractError, "invalid chat history response"):
                    asyncio.run(connector.get_messages("chat-contract"))

    def test_first_message_failure_rolls_back_new_chat(self) -> None:
        connector = FakeYandexConnector(
            [_chat("new-rollback")],
            {"new-rollback": [_message("new-rollback", "m-1")]},
        )
        with patch.object(repo, "_add_message_conn", side_effect=sqlite3.IntegrityError("injected")):
            result = self._sync(connector)

        self.assertFalse(result["ok"])
        self.assertIsNone(repo.get_chat_by_external("yandex", "new-rollback"))

    def test_materialization_reconciles_provider_echo_with_provisional_crm_outbound(self) -> None:
        chat = _chat("legacy-outbound-race")
        chat_id = self._create_chat(chat)
        provisional_id = repo.add_message(
            chat_id,
            "outbound",
            "Seller reply",
            author="operator",
            raw={"_crm_sent_from_crm": True},
            created_at="2026-01-01T10:00:00Z",
        )
        provider_echo = _message(
            "legacy-outbound-race",
            "provider-message-id",
            direction="outbound",
            created_at="2026-01-01T10:00:01Z",
            text="Seller reply",
        )

        materialized_id = repo.materialize_yandex_chat_with_messages(
            ChatCreate(
                marketplace="yandex",
                external_chat_id=chat.external_chat_id,
                customer_name=chat.customer_name,
                status=chat.status,  # type: ignore[arg-type]
                metadata=chat.metadata,
            ),
            [provider_echo],
        )

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT id, external_message_id FROM messages WHERE chat_id=? ORDER BY id",
                (chat_id,),
            ).fetchall()
        self.assertEqual(chat_id, materialized_id)
        self.assertEqual([(provisional_id, "provider-message-id")], [(int(row["id"]), row["external_message_id"]) for row in rows])

    def test_legacy_shell_retries_once_and_later_materializes_atomically(self) -> None:
        chat = _chat("legacy-shell")
        chat_id = self._create_chat(chat)
        self._set_updated_at(chat_id, "2001-01-01 00:00:00")
        empty_connector = FakeYandexConnector([chat], {"legacy-shell": []})

        empty_result = self._sync(empty_connector)

        self.assertTrue(empty_result["ok"])
        self.assertEqual("2001-01-01 00:00:00", self._updated_at(chat_id))
        self.assertFalse(repo.chat_has_messages(chat_id))
        self.assertEqual(["legacy-shell"], empty_connector.history_calls)

        filled_connector = FakeYandexConnector(
            [chat],
            {"legacy-shell": [_message("legacy-shell", "m-1"), _message("legacy-shell", "m-2", created_at="2026-01-01T11:00:00Z")]},
        )
        filled_result = self._sync(filled_connector)

        self.assertTrue(filled_result["ok"])
        self.assertEqual(2, filled_result["messages_count"])
        self.assertTrue(repo.chat_has_messages(chat_id))
        self.assertEqual(["legacy-shell"], filled_connector.history_calls)

    def test_identical_sync_preserves_updated_at_and_fetches_history_once(self) -> None:
        chat = _chat("stable-real")
        chat_id = self._create_chat(chat)
        message = _message("stable-real", "stable-m")
        repo.add_message(chat_id, message.direction, message.text, message.author, message.external_message_id, message.raw, message.created_at)
        self._set_updated_at(chat_id, "2002-02-02 02:02:02")
        connector = FakeYandexConnector([chat], {"stable-real": [message]})

        result = self._sync(connector)

        self.assertTrue(result["ok"])
        self.assertEqual("2002-02-02 02:02:02", self._updated_at(chat_id))
        self.assertEqual(["stable-real"], connector.history_calls)

    def test_cache_repair_noop_preserves_updated_at(self) -> None:
        chat_id = self._create_chat(_chat("stable-cache"))
        repo.add_message(chat_id, "inbound", "Latest", external_message_id="cache-m", created_at="2026-01-01T12:00:00Z")
        self._set_updated_at(chat_id, "2003-03-03 03:03:03")

        repo.repair_chat_last_message_cache()

        self.assertEqual("2003-03-03 03:03:03", self._updated_at(chat_id))

    def test_list_hides_yandex_shells_without_deleting_tasks_or_assignment(self) -> None:
        assigned_id = self._create_chat(_chat("assigned-shell"))
        task_id = self._create_chat(_chat("task-shell"))
        with db.get_connection() as conn:
            conn.execute("UPDATE chats SET assigned_to='operator' WHERE id=?", (assigned_id,))
            conn.execute("INSERT INTO tasks (chat_id, title) VALUES (?, 'Follow up')", (task_id,))

        visible_ids = {item["id"] for item in repo.list_chats(marketplace="yandex")}

        self.assertNotIn(assigned_id, visible_ids)
        self.assertNotIn(task_id, visible_ids)
        with db.get_connection() as conn:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) AS n FROM chats WHERE id IN (?, ?)", (assigned_id, task_id)).fetchone()["n"])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE chat_id=?", (task_id,)).fetchone()["n"])

    def test_yandex_actionability_requires_status_and_latest_inbound_message(self) -> None:
        no_message_id = self._create_chat(_chat("no-message", provider_status="NEW"))
        inbound_id = self._create_chat(_chat("inbound", provider_status="WAITING_FOR_PARTNER"))
        outbound_id = self._create_chat(_chat("outbound", provider_status="NEW"))
        non_actionable_id = self._create_chat(_chat("waiting-customer", provider_status="WAITING_FOR_CUSTOMER"))
        repo.add_message(inbound_id, "inbound", "Buyer", external_message_id="in-1", created_at="2026-01-01T10:00:00Z")
        repo.add_message(outbound_id, "outbound", "Seller", external_message_id="out-1", created_at="2026-01-01T10:00:00Z")
        repo.add_message(non_actionable_id, "inbound", "Buyer", external_message_id="in-2", created_at="2026-01-01T10:00:00Z")

        cases = {
            no_message_id: False,
            inbound_id: True,
            outbound_id: False,
            non_actionable_id: False,
        }
        for chat_id, expected in cases.items():
            item = repo.get_chat_summary(chat_id)
            self.assertIsNotNone(item)
            self.assertIs(expected, item["yandex_needs_partner_reply"])
            self.assertIs(expected, item["metadata"]["_sync_hint"]["yandex_needs_partner_reply"])

    def test_list_order_uses_actual_message_time_then_chat_id_not_updated_at(self) -> None:
        older_id = self._create_chat(_chat("older"))
        newer_low_id = self._create_chat(_chat("newer-low-id"))
        newer_high_id = self._create_chat(_chat("newer-high-id"))
        repo.add_message(older_id, "inbound", "Old", external_message_id="old", created_at="2026-01-01T09:00:00Z")
        repo.add_message(newer_low_id, "inbound", "New A", external_message_id="new-a", created_at="2026-01-01T10:00:00Z")
        repo.add_message(newer_high_id, "inbound", "New B", external_message_id="new-b", created_at="2026-01-01T10:00:00Z")
        self._set_updated_at(older_id, "2099-01-01 00:00:00")
        self._set_updated_at(newer_low_id, "2000-01-01 00:00:00")
        self._set_updated_at(newer_high_id, "2000-01-01 00:00:00")

        listed = repo.list_chats(marketplace="yandex")

        self.assertEqual([newer_high_id, newer_low_id, older_id], [item["id"] for item in listed])


if __name__ == "__main__":
    unittest.main()

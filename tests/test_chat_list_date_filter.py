from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

import httpx

import test_regression_foundation as foundation
from app import db
from app import repository as repo
from app.schemas import ChatCreate


main = foundation.main


class ChatListDateFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.user = repo.create_user(
            "chat-list-date-manager",
            "chat-list-date-manager-password",
            "Chat List Date Manager",
            "manager",
        )
        self.inside_chat_id = self._create_chat(
            "inside-range",
            [("inside", "2026-08-06T10:15:00Z")],
        )
        self.outside_chat_id = self._create_chat(
            "latest-outside-range",
            [
                ("old-inside", "2026-08-06T12:00:00Z"),
                ("latest-outside", "2026-08-07T08:00:00Z"),
            ],
        )
        self.boundary_chat_id = self._create_chat(
            "at-local-day-start",
            [("boundary", "2026-08-05T21:00:00Z")],
        )

    def _create_chat(self, external_chat_id: str, messages: list[tuple[str, str]]) -> int:
        chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id=external_chat_id,
                customer_name=external_chat_id,
                metadata={"synthetic": True},
            )
        )
        for external_message_id, created_at in messages:
            repo.add_message(
                chat_id,
                "inbound",
                external_message_id,
                author="customer",
                external_message_id=external_message_id,
                raw={"synthetic": True},
                created_at=created_at,
            )
        return chat_id

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_repository_filters_chats_by_latest_message_timestamp(self) -> None:
        page = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            last_message_created_from="2026-08-05T21:00:00Z",
            last_message_created_to="2026-08-06T21:00:00Z",
            limit=30,
        )

        self.assertEqual(
            [self.inside_chat_id, self.boundary_chat_id],
            [int(chat["id"]) for chat in page["items"]],
        )
        self.assertEqual(2, page["total"])
        self.assertNotIn(self.outside_chat_id, [int(chat["id"]) for chat in page["items"]])

    def test_api_converts_browser_timezone_range_for_chat_list(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="chat-list-date-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get(
                    "/api/chats",
                    params={
                        "date_from": "2026-08-06",
                        "date_to": "2026-08-06",
                        "timezone_offset_minutes": -180,
                        "paginated": "true",
                        "limit": 30,
                    },
                )

        response = asyncio.run(exercise())

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [self.inside_chat_id, self.boundary_chat_id],
            [int(chat["id"]) for chat in response.json()["items"]],
        )

    def test_api_rejects_partial_reversed_or_impossible_chat_date_range(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="chat-list-date-test")

        async def exercise(params):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get("/api/chats", params=params)

        partial = asyncio.run(exercise({"date_from": "2026-08-06"}))
        reversed_range = asyncio.run(
            exercise({"date_from": "2026-08-07", "date_to": "2026-08-06"})
        )
        impossible_offset = asyncio.run(
            exercise(
                {
                    "date_from": "2026-08-06",
                    "date_to": "2026-08-06",
                    "timezone_offset_minutes": 900,
                }
            )
        )

        self.assertEqual(422, partial.status_code)
        self.assertEqual(422, reversed_range.status_code)
        self.assertEqual(422, impossible_offset.status_code)


class ChatListDateFilterUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def test_calendar_is_in_chat_list_and_absent_from_open_chat_header(self) -> None:
        self.assertIn('id="chatDateFilterBtn"', self.html)
        self.assertIn('id="chatDateFilterFromInput" type="date"', self.html)
        self.assertIn('id="chatDateFilterToInput" type="date"', self.html)
        self.assertNotIn('id="messageDateFilterBtn"', self.html)
        self.assertNotIn('id="messageDateFilterPopover"', self.html)

    def test_chat_list_request_owns_date_range_and_message_request_does_not(self) -> None:
        self.assertIn("params.set('date_from', currentChatListDateFrom)", self.source)
        self.assertIn("params.set('date_to', currentChatListDateTo)", self.source)
        self.assertIn("params.set('timezone_offset_minutes', String(new Date().getTimezoneOffset()))", self.source)
        self.assertIn("currentChatListDateFrom,\n    currentChatListDateTo,", self.source)
        self.assertNotIn("message_date_from", self.source)
        self.assertNotIn("message_date_to", self.source)
        self.assertIn("function chatMessagesRequestUrl(chatId, messagesLimit)", self.source)
        self.assertEqual(1, self.source.count("params.set('messages_limit', String(messagesLimit))"))

    def test_chat_date_range_reuses_shared_validation_and_resets_lazy_feed(self) -> None:
        self.assertIn("const [normalizedFrom, normalizedTo] = validateDateRangeValues(fromValue, toValue);", self.source)
        self.assertIn("resetChatListFeed();\n  syncChatDateFilterUi();", self.source)
        self.assertIn("bind('chatDateFilterFromInput', 'change'", self.source)
        self.assertIn("bind('chatDateFilterToInput', 'change'", self.source)


if __name__ == "__main__":
    unittest.main()

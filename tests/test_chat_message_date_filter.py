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


class ChatMessageDateFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.user = repo.create_user(
            "message-date-manager",
            "message-date-manager-password",
            "Message Date Manager",
            "manager",
        )
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="message-date-chat",
                customer_name="Message Date Customer",
                metadata={"synthetic": True},
            )
        )
        for external_id, created_at in (
            ("before-local-day", "2026-08-05T20:59:59Z"),
            ("local-day-start", "2026-08-05T21:00:00Z"),
            ("local-day-middle", "2026-08-06T10:15:00Z"),
            ("next-local-day", "2026-08-06T21:00:00Z"),
        ):
            repo.add_message(
                self.chat_id,
                "inbound",
                external_id,
                author="customer",
                external_message_id=external_id,
                raw={"synthetic": True},
                created_at=created_at,
            )

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_repository_filters_by_half_open_utc_range(self) -> None:
        chat = repo.get_chat(
            self.chat_id,
            messages_limit=120,
            current_user_id=int(self.user["id"]),
            message_created_from="2026-08-05T21:00:00Z",
            message_created_to="2026-08-06T21:00:00Z",
        )

        self.assertIsNotNone(chat)
        self.assertEqual(
            ["local-day-start", "local-day-middle"],
            [message["external_message_id"] for message in chat["messages"]],
        )

    def test_api_converts_browser_timezone_offset_to_inclusive_local_range(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="message-date-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get(
                    f"/api/chats/{self.chat_id}",
                    params={
                        "message_date_from": "2026-08-05",
                        "message_date_to": "2026-08-06",
                        "timezone_offset_minutes": -180,
                        "messages_limit": 120,
                    },
                )

        response = asyncio.run(exercise())

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            ["before-local-day", "local-day-start", "local-day-middle"],
            [message["external_message_id"] for message in response.json()["messages"]],
        )

    def test_api_rejects_partial_or_reversed_date_range(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="message-date-test")

        async def exercise(params):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get(f"/api/chats/{self.chat_id}", params=params)

        partial = asyncio.run(exercise({"message_date_from": "2026-08-06"}))
        reversed_range = asyncio.run(
            exercise(
                {
                    "message_date_from": "2026-08-07",
                    "message_date_to": "2026-08-06",
                }
            )
        )
        self.assertEqual(422, partial.status_code)
        self.assertEqual(422, reversed_range.status_code)

    def test_api_rejects_impossible_timezone_offset(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="message-date-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                return await client.get(
                    f"/api/chats/{self.chat_id}",
                    params={
                        "message_date_from": "2026-08-06",
                        "message_date_to": "2026-08-06",
                        "timezone_offset_minutes": 900,
                    },
                )

        response = asyncio.run(exercise())
        self.assertEqual(422, response.status_code)


class ChatMessageDateFilterUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def test_calendar_range_control_and_single_request_builder_are_used(self) -> None:
        self.assertIn('id="messageDateFilterBtn"', self.html)
        self.assertIn('id="messageDateFilterFromInput" type="date"', self.html)
        self.assertIn('id="messageDateFilterToInput" type="date"', self.html)
        self.assertIn('id="messageDateFilterApplyBtn"', self.html)
        self.assertIn('id="messageDateFilterClearBtn"', self.html)
        self.assertIn("function chatMessagesRequestUrl(chatId, messagesLimit)", self.source)
        self.assertIn("params.set('message_date_from', currentChatMessageDateFrom)", self.source)
        self.assertIn("params.set('message_date_to', currentChatMessageDateTo)", self.source)
        self.assertIn("params.set('timezone_offset_minutes', String(new Date().getTimezoneOffset()))", self.source)
        self.assertEqual(2, self.source.count("api(chatMessagesRequestUrl(chatId, messagesLimit)"))
        self.assertNotIn("params.set('message_date',", self.source)
        self.assertNotIn("date(message.created_at)", self.source)

    def test_filter_resets_only_when_switching_to_another_chat(self) -> None:
        reset_block = """if (previousChatId !== currentChatId) {
    selectedAiMessageId = null;
    currentChatMessageDateFrom = '';
    currentChatMessageDateTo = '';"""
        self.assertIn(reset_block, self.source)
        self.assertIn("if (currentChatId) await openChat(currentChatId, { syncRoute: false });", self.source)

    def test_range_validation_is_canonical_and_not_duplicated_in_request_builder(self) -> None:
        self.assertIn("if (!normalizedFrom || !normalizedTo)", self.source)
        self.assertIn("if (normalizedFrom > normalizedTo)", self.source)
        self.assertIn("function hasActiveMessageDateRange()", self.source)
        self.assertIn("bind('messageDateFilterFromInput', 'change', syncMessageDateFilterDraftBounds)", self.source)
        self.assertIn("bind('messageDateFilterToInput', 'change', syncMessageDateFilterDraftBounds)", self.source)
        self.assertNotIn("currentChatMessageDateFilter", self.source)


if __name__ == "__main__":
    unittest.main()

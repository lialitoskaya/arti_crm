from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


main = foundation.main


class ChatListPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.user = repo.create_user(
            "pagination-manager",
            "pagination-manager-password",
            "Pagination Manager",
            "manager",
        )
        start = datetime.now(timezone.utc) - timedelta(seconds=205)
        self.chat_ids: list[int] = []
        for index in range(205):
            chat_id = repo.upsert_chat(
                ChatCreate(
                    marketplace="ozon",
                    external_chat_id=f"pagination-chat-{index:03d}",
                    customer_name=f"Synthetic {index:03d}",
                    metadata={"synthetic": True},
                )
            )
            repo.add_message(
                chat_id,
                "inbound",
                f"message {index:03d}",
                author="synthetic-customer",
                external_message_id=f"pagination-message-{index:03d}",
                raw={"synthetic": True},
                created_at=(start + timedelta(seconds=index)).isoformat(),
            )
            self.chat_ids.append(chat_id)

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_repository_returns_bounded_pages_and_canonical_counts(self) -> None:
        first = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            limit=100,
            offset=0,
        )
        last = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            limit=100,
            offset=200,
        )

        self.assertEqual(205, first["total"])
        self.assertEqual(205, first["unread_total"])
        self.assertEqual(100, len(first["items"]))
        self.assertTrue(first["has_more"])
        self.assertFalse(first["has_previous"])
        self.assertEqual(self.chat_ids[-1], first["items"][0]["id"])

        self.assertEqual(200, last["offset"])
        self.assertEqual(5, len(last["items"]))
        self.assertFalse(last["has_more"])
        self.assertTrue(last["has_previous"])
        self.assertEqual(self.chat_ids[0], last["items"][-1]["id"])

    def test_out_of_range_offset_is_moved_to_last_page(self) -> None:
        page = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            limit=100,
            offset=9999,
        )

        self.assertEqual(200, page["offset"])
        self.assertEqual(5, len(page["items"]))

    def test_api_keeps_legacy_list_and_adds_opt_in_paginated_contract(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="pagination-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                legacy = await client.get("/api/chats")
                paginated = await client.get(
                    "/api/chats",
                    params={"paginated": "true", "limit": 100, "offset": 100},
                )
                return legacy, paginated

        legacy, paginated = asyncio.run(exercise())

        self.assertEqual(200, legacy.status_code)
        self.assertIsInstance(legacy.json(), list)
        self.assertEqual(205, len(legacy.json()))
        self.assertEqual(200, paginated.status_code)
        payload = paginated.json()
        self.assertEqual(205, payload["total"])
        self.assertEqual(100, payload["offset"])
        self.assertEqual(100, len(payload["items"]))


class ChatListPaginationUiContractTests(unittest.TestCase):
    def test_frontend_uses_server_pagination_and_bounded_pager(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("const CHAT_LIST_PAGE_SIZE = 100;", source)
        self.assertIn("params.set('paginated', 'true')", source)
        self.assertIn("params.set('offset', String(chatListOffset))", source)
        self.assertIn("function renderChatListPager()", source)
        self.assertIn('id="chatListPager"', html)
        self.assertIn('id="chatListPrevBtn"', html)
        self.assertIn('id="chatListNextBtn"', html)


if __name__ == "__main__":
    unittest.main()
